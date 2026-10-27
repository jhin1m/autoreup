"""Download one HTTP file over many connections at once, by byte range.

Bilibili's CDN caps each connection (~150-200 KB/s measured from Vietnam, on every
host), so one stream crawls however good the host is: on one 516 MB episode 1
connection gave ~0.4 MB/s, 8 gave 1.5 MB/s, 16 gave 3 MB/s. BBDown, yutto and
Bili23-Downloader all split the file into ranges fetched in parallel; this does the same.

The file is written in place into ``<dest>.part``; each finished chunk is appended to
``<dest>.chunks``, so a retry of the job resumes where it stopped. A chunk that drops
mid-way (the CDN closes some connections early) resumes from its last byte.

The same file can be offered on several hosts (``urls``): some byte ranges are broken on
one host only — Akamai closed every request for one 4 MiB slice of an episode at once,
on every retry and with a freshly signed URL, while a mirror served it — so a chunk that
keeps failing moves on to the next host.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

import httpx

WORKERS = 16
CHUNK_BYTES = 4 * 1024 * 1024
_CHUNK_RETRIES = 12  # attempts in a row with no bytes, across hosts, before giving up
_HOST_FAILURES = 3  # attempts in a row with no bytes before a chunk tries the next host
_BACKOFF = 1.0  # seconds before the 2nd attempt on a host; doubles for the 3rd
_TIMEOUT = httpx.Timeout(15.0, connect=10.0)  # a stalled read becomes a retry
_SPEED_WINDOW = 5.0  # seconds of history behind the reported speed
_REPORT_EVERY = 0.5


class RangeUnsupported(RuntimeError):
    """The server ignores Range requests: the caller must download it another way."""


def total_size(urls: list[str], headers: dict) -> int:
    """File size from a one-byte range request, from the first host that answers;
    RangeUnsupported when ranges don't work."""
    for i, url in enumerate(urls):
        try:
            return _total_size(url, headers)
        except (httpx.TransportError, httpx.HTTPStatusError):
            if i == len(urls) - 1:
                raise
    raise ValueError("no urls")


def _total_size(url: str, headers: dict) -> int:
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as response:
            response.raise_for_status()
            content_range = response.headers.get("Content-Range", "")
            if response.status_code != 206 or "/" not in content_range:
                raise RangeUnsupported(f"no range support (HTTP {response.status_code})")
            size = content_range.rsplit("/", 1)[1]
            if not size.isdigit():
                raise RangeUnsupported(f"unknown size: {content_range!r}")
            return int(size)


def download(
    urls: list[str],
    dest: Path,
    headers: dict,
    on_progress: Callable[[int, int, float | None], None] | None = None,
    workers: int = WORKERS,
    chunk_bytes: int = CHUNK_BYTES,
) -> Path:
    """Fetch the file at ``urls`` (one per host, preferred first) into ``dest``. ``on_progress(downloaded, total, speed)`` runs on the
    calling thread; an exception it raises (a canceled job) stops every connection and
    propagates, leaving the .part and its chunk ledger to resume from."""
    if dest.exists():
        return dest  # only a complete file is ever renamed to dest
    total = total_size(urls, headers)
    part = dest.with_name(dest.name + ".part")
    ledger = dest.with_name(dest.name + ".chunks")
    done = _read_ledger(ledger, part, total, chunk_bytes)
    if not done:
        ledger.write_text(f"{total} {chunk_bytes}\n")
    with open(part, "r+b" if part.exists() else "w+b") as fh:
        fh.truncate(total)

    pending: queue.SimpleQueue[tuple[int, int]] = queue.SimpleQueue()
    for start in range(0, total, chunk_bytes):
        if start not in done:
            pending.put((start, min(start + chunk_bytes, total) - 1))
    got = [sum(min(chunk_bytes, total - s) for s in done)]
    lock = threading.Lock()
    stop = threading.Event()
    errors: list[BaseException] = []

    def add(n: int) -> None:
        with lock:
            got[0] += n

    def finish(start: int) -> None:
        with lock, open(ledger, "a") as fh:
            fh.write(f"{start}\n")

    def worker() -> None:
        try:
            with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=headers) as client, open(part, "r+b") as fh:
                while not stop.is_set():
                    try:
                        start, end = pending.get_nowait()
                    except queue.Empty:
                        return
                    if _fetch_chunk(client, fh, urls, start, end, add, stop):
                        finish(start)
        except BaseException as exc:  # noqa: BLE001 - handed to the calling thread
            errors.append(exc)
            stop.set()

    threads = [threading.Thread(target=worker, name=f"range-{i}", daemon=True) for i in range(workers)]
    for thread in threads:
        thread.start()
    history: deque[tuple[float, int]] = deque()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(_REPORT_EVERY)
            if on_progress is not None:
                now, current = time.monotonic(), got[0]
                history.append((now, current))
                while now - history[0][0] > _SPEED_WINDOW:
                    history.popleft()
                elapsed = now - history[0][0]
                on_progress(current, total, (current - history[0][1]) / elapsed if elapsed > 0 else None)
    except BaseException:
        stop.set()
        raise
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    if errors:
        raise errors[0]
    if got[0] != total:
        raise RuntimeError(f"download incomplete: {got[0]} of {total} bytes")
    part.replace(dest)
    ledger.unlink(missing_ok=True)
    if on_progress is not None:
        on_progress(total, total, None)
    return dest


def _fetch_chunk(client: httpx.Client, fh, urls: list[str], start: int, end: int, add, stop: threading.Event) -> bool:
    """Write bytes ``start..end`` at their offset, resuming after a dropped connection.
    False when stopped early; raises after ``_CHUNK_RETRIES`` attempts in a row that got
    no bytes. After ``_HOST_FAILURES`` of them on one host (or one 4xx: that host refuses
    the URL) the chunk moves to the next host. A drop after some bytes is not a failure:
    Akamai cut ~2 of 3 connections mid-chunk under load, still advancing every time."""
    pos = start
    host = failures = host_failures = 0
    while not stop.is_set():
        attempt_start = pos
        try:
            with client.stream("GET", urls[host % len(urls)], headers={"Range": f"bytes={pos}-{end}"}) as response:
                if response.status_code != 206:
                    response.raise_for_status()
                    raise RangeUnsupported(f"range ignored (HTTP {response.status_code})")
                for data in response.iter_bytes():
                    data = data[: end + 1 - pos]  # never write past this chunk
                    fh.seek(pos)
                    fh.write(data)
                    pos += len(data)
                    add(len(data))
                    if stop.is_set():
                        return False
                    if pos > end:
                        return True
            if pos > end:
                return True
            raise httpx.ReadError("connection closed before the chunk ended")
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            error = exc
        if pos > attempt_start:
            failures = host_failures = 0  # it was moving: reconnect to the same host at once
            continue
        failures += 1
        host_failures += 1
        if failures > _CHUNK_RETRIES:
            raise error
        refused = isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500
        if refused or host_failures >= _HOST_FAILURES:
            host, host_failures = host + 1, 0
        else:
            stop.wait(_BACKOFF * 2 ** (host_failures - 1))  # 1s, 2s, then the next host
    return False


def _read_ledger(ledger: Path, part: Path, total: int, chunk_bytes: int) -> set[int]:
    """Chunk starts already on disk, if the ledger matches this file and chunk size."""
    try:
        header, *lines = ledger.read_text().splitlines()
        if header.split() != [str(total), str(chunk_bytes)] or part.stat().st_size != total:
            return set()
        return {int(line) for line in lines if line.strip().isdigit()}
    except (OSError, ValueError):
        return set()

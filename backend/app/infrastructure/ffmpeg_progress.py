"""Run a long ffmpeg render with live progress and a stall watchdog.

A 6.5 h episode renders for over an hour. Without ``-progress`` the UI showed one static
line for all of it, and once ffmpeg hung at the very end (every worker thread gone, the
file never finalized) nothing noticed. This runner reports how far the render is and
kills ffmpeg when its output clock stops moving."""
import math
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from backend.app.infrastructure.process import ProcessError, run_command

# No progress for this long = hung. A healthy render advances every stats period; even
# the slowest legit start (opening inputs, building the graph) takes seconds.
STALL_SECONDS = 300.0
# How often ffmpeg writes a progress block. Every block is a dozen lines kept in memory
# for the whole render, so not the 0.5 s default.
_STATS_PERIOD = "2"
# One "key=value" line of a -progress block ("out_time_us=1234", "speed=4.1x").
_PROGRESS_LINE = re.compile(r"^[a-z0-9_]+=\S*$")


class RenderStalled(ProcessError):
    pass


def kill_stale_renders(output: str) -> int:
    """Kill any ffmpeg still writing ``output``; returns how many. A render runs in its
    own session (so Stop can kill it), which also means stopping the server doesn't
    reach it: it outlives the server, and the job resumed on restart would render into
    the same file at the same time (seen: two encoders interleaving one 12 GB file)."""
    listing = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, check=False).stdout
    killed = 0
    for row in listing.splitlines():
        pid, _, command = row.strip().partition(" ")
        if command.startswith("ffmpeg ") and command.endswith(" " + output) and int(pid) != os.getpid():
            try:
                os.kill(int(pid), signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                pass
    return killed


def run_ffmpeg_progress(
    command: list[str],
    total_seconds: float,
    report: Callable[[dict], None] | None = None,
    cancel: threading.Event | None = None,
    stall_seconds: float = STALL_SECONDS,
) -> list[str]:
    """Run ``command`` (an ffmpeg invocation ending in its output path; ``-progress`` is
    added here) and return its output lines. An earlier render left writing the same
    output is killed first. ``report`` gets {"done", "total", "unit", "eta"} in minutes
    of output as the render advances; ``total_seconds`` is the output's length (0 =
    unknown, no counts).
    Setting ``cancel`` kills the render; so does no progress for ``stall_seconds``, which
    raises RenderStalled."""
    stop = threading.Event()  # set by the watchdog: the user's cancel or a stall
    finished = threading.Event()
    started = last_move = time.monotonic()
    position = 0.0
    stalled = False

    def on_line(line: str) -> None:
        nonlocal position, last_move
        key, _, value = line.partition("=")
        if key != "out_time_us":
            return
        try:
            seconds = int(value) / 1_000_000
        except ValueError:
            return  # "N/A" until the first frame is out
        if seconds <= position:
            return
        position, last_move = seconds, time.monotonic()
        if report is not None and total_seconds > 0:
            rate = position / (last_move - started)  # output seconds per wall second
            report({
                "done": min(int(position // 60), math.ceil(total_seconds / 60)),
                "total": math.ceil(total_seconds / 60),
                "unit": "phút video",
                "eta": max(0.0, total_seconds - position) / rate if rate > 0 else None,
            })

    def watch() -> None:
        nonlocal stalled
        while not finished.wait(1.0):
            if cancel is not None and cancel.is_set():
                stop.set()
                return
            if time.monotonic() - last_move > stall_seconds:
                stalled = True
                stop.set()
                return

    kill_stale_renders(command[-1])
    threading.Thread(target=watch, daemon=True).start()
    try:
        return run_command(
            [command[0], "-progress", "pipe:1", "-stats_period", _STATS_PERIOD, *command[1:]],
            cwd=Path.cwd(), on_line=on_line, cancel=stop,
        )
    except ProcessError as exc:
        if stalled:
            raise RenderStalled(
                f"ffmpeg đứng im {stall_seconds / 60:.0f} phút ở phút {position / 60:.0f}"
                f"/{total_seconds / 60:.0f} của video — đã dừng render, hãy chạy lại"
            ) from exc
        # the error tail is buried under progress blocks: keep only ffmpeg's own lines
        raise ProcessError("\n".join(line for line in str(exc).splitlines() if not _PROGRESS_LINE.match(line))) from exc
    finally:
        finished.set()

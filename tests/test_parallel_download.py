import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from backend.app.infrastructure import bilibili_cdn, parallel_download
from backend.app.infrastructure.downloader import YtDlpDownloader

BODY = os.urandom(300_000)
CHUNK = 64 * 1024


@pytest.fixture
def server():
    """A local HTTP server serving BODY with Range support. ``state["drops"]`` maps a
    chunk index to how many of its responses end early (after a third of the bytes);
    ``state["ranges"]`` records requests; chunks in ``state["broken"]`` are refused on
    the ``/bad/`` path (closed with no response), as one CDN host did."""
    state = {"ranges": [], "drops": {}, "no_ranges": False, "broken": set()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            header = self.headers.get("Range")
            if state["no_ranges"] or not header:
                self.send_response(200)
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)
                return
            start, end = (int(x) for x in header.removeprefix("bytes=").split("-"))
            if self.path.startswith("/bad/") and start // CHUNK in state["broken"]:
                self.close_connection = True
                return
            state["ranges"].append((start, end))
            data = BODY[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(BODY)}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if state["drops"].get(start // CHUNK):
                state["drops"][start // CHUNK] -= 1
                self.wfile.write(data[: max(1, len(data) // 3)])  # then the connection closes short
                self.close_connection = True
                return
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{httpd.server_port}/v.m4s"
    yield state
    httpd.shutdown()


def test_the_file_arrives_whole_over_several_connections(server, tmp_path):
    seen: list[tuple] = []
    dest = parallel_download.download([server["url"]], tmp_path / "v.mp4", {}, lambda *a: seen.append(a), workers=4, chunk_bytes=CHUNK)

    assert dest.read_bytes() == BODY
    assert len({start for start, _ in server["ranges"]}) > 4  # split into many ranges
    assert seen[-1] == (len(BODY), len(BODY), None)
    assert not (tmp_path / "v.mp4.part").exists() and not (tmp_path / "v.mp4.chunks").exists()


def test_a_connection_dropped_mid_chunk_resumes_from_its_last_byte(server, tmp_path):
    server["drops"][1] = 1
    dest = parallel_download.download([server["url"]], tmp_path / "v.mp4", {}, workers=2, chunk_bytes=CHUNK)

    assert dest.read_bytes() == BODY
    retry = [r for r in server["ranges"] if CHUNK < r[0] < 2 * CHUNK]
    assert retry and retry[0][1] == 2 * CHUNK - 1  # asked only for the missing tail


def test_a_chunk_cut_short_again_and_again_still_finishes_while_it_advances(server, tmp_path):
    # More drops than the retry budget: each one still delivered bytes, so none counts.
    server["drops"][1] = parallel_download._CHUNK_RETRIES + 5
    dest = parallel_download.download([server["url"]], tmp_path / "v.mp4", {}, workers=2, chunk_bytes=CHUNK)
    assert dest.read_bytes() == BODY


def test_a_stopped_download_resumes_without_refetching_finished_chunks(server, tmp_path):
    def cancel_midway(downloaded, total, speed):
        if downloaded >= 2 * CHUNK:
            raise RuntimeError("canceled")

    with pytest.raises(RuntimeError, match="canceled"):
        parallel_download.download([server["url"]], tmp_path / "v.mp4", {}, cancel_midway, workers=1, chunk_bytes=CHUNK)
    assert (tmp_path / "v.mp4.chunks").exists()
    server["ranges"].clear()

    dest = parallel_download.download([server["url"]], tmp_path / "v.mp4", {}, workers=2, chunk_bytes=CHUNK)
    assert dest.read_bytes() == BODY
    assert (0, CHUNK - 1) not in server["ranges"]  # the first chunk was already on disk


def test_a_range_one_host_refuses_comes_from_the_next_host(server, tmp_path):
    server["broken"] = {2}
    bad = server["url"].replace("/v.m4s", "/bad/v.m4s")
    with patch.object(parallel_download, "_BACKOFF", 0.0):
        dest = parallel_download.download([bad, server["url"]], tmp_path / "v.mp4", {}, workers=2, chunk_bytes=CHUNK)
    assert dest.read_bytes() == BODY


def test_a_server_without_range_support_is_reported(server, tmp_path):
    server["no_ranges"] = True
    with pytest.raises(parallel_download.RangeUnsupported):
        parallel_download.download([server["url"]], tmp_path / "v.mp4", {})


def test_bilibili_streams_download_in_parallel_into_the_job_video(server, tmp_path):
    """yt-dlp only extracts and picks the format; the bytes come over many connections."""

    class FakeYDL:
        def __init__(self, options):
            self.params = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download, process=True):
            assert not download
            return {"title": "t", "formats": [{"format_id": "100026", "url": server["url"]}]}

        def process_ie_result(self, raw, download):
            assert not download  # yt-dlp's single-connection download is not used
            fmt = {**raw["formats"][0], "ext": "mp4", "protocol": "http", "http_headers": {"Referer": "x"}}
            return {**raw, "requested_formats": [fmt]}

    seen: list[dict] = []
    with patch.dict(sys.modules, {"yt_dlp": types.SimpleNamespace(YoutubeDL=FakeYDL)}), \
            patch.object(bilibili_cdn, "retarget_formats", return_value=("upos-sz-mirrorali.bilivideo.com", {})):
        result = YtDlpDownloader(tmp_path).download("https://www.bilibili.com/video/BV1", video_id="vid", on_progress=seen.append)

    assert result["path"] == tmp_path / "vid.mp4"
    assert result["path"].read_bytes() == BODY
    assert result["cdn_host"] == "upos-sz-mirrorali.bilivideo.com"
    assert seen[-1]["downloaded_bytes"] == len(BODY) == seen[-1]["total_bytes"]

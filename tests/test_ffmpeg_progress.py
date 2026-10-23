import os
import shutil
import subprocess
import time

import pytest

from backend.app.infrastructure.ffmpeg_progress import RenderStalled, run_ffmpeg_progress

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_a_render_reports_how_far_it_is(tmp_path):
    reports = []
    run_ffmpeg_progress(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=90",
         "-c:v", "mpeg4", str(tmp_path / "out.mp4")],
        90.0, report=reports.append,
    )
    assert reports, "no progress while rendering"
    assert reports[-1]["total"] == 2 and reports[-1]["unit"] == "phút video"
    assert reports[-1]["done"] == 1  # 90 s rendered = minute 1 of 2
    assert all(r["eta"] is None or r["eta"] >= 0 for r in reports)


def test_a_render_that_stops_advancing_is_killed(tmp_path):
    # the episode render once hung at its very end for good: nothing noticed
    fifo = tmp_path / "never"
    os.mkfifo(fifo)  # ffmpeg blocks opening it: no frame ever comes out
    started = time.monotonic()
    with pytest.raises(RenderStalled, match="đứng im"):
        run_ffmpeg_progress(["ffmpeg", "-y", "-v", "error", "-i", str(fifo), str(tmp_path / "out.mp4")], 60.0, stall_seconds=1.5)
    assert time.monotonic() - started < 10


def test_a_failed_render_keeps_ffmpegs_error_not_the_progress_lines(tmp_path):
    with pytest.raises(Exception) as caught:
        run_ffmpeg_progress(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1",
             "-vf", "no_such_filter", str(tmp_path / "out.mp4")],
            1.0,
        )
    message = str(caught.value)
    assert "no_such_filter" in message
    assert "out_time_us=" not in message


def test_a_render_left_behind_by_a_dead_server_is_killed_first(tmp_path):
    # the old render outlived a server restart and wrote into the resumed job's file
    fifo, out = tmp_path / "never", tmp_path / "out.mp4"
    os.mkfifo(fifo)
    stale = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-i", str(fifo), str(out)], stdin=subprocess.DEVNULL, start_new_session=True)
    try:
        time.sleep(0.5)  # until ps lists it
        run_ffmpeg_progress(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=1", "-c:v", "mpeg4", str(out)],
            1.0,
        )
        assert stale.wait(timeout=5) == -9
    finally:
        stale.kill()

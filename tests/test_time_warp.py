import random
import re
import shutil
import subprocess

import pytest

from backend.app.dubbing.time_warp import TimeWarp, WarpSegment


def test_identity_without_segments():
    warp = TimeWarp()
    assert not warp
    assert warp.to_output(12.345) == 12.345
    assert warp.to_source(12.345) == 12.345
    assert warp.extra_seconds() == 0
    assert warp.setpts_filter() == ""
    assert TimeWarp.from_json(None).segments == []


def test_one_segment_maps_before_inside_and_after():
    warp = TimeWarp([WarpSegment(10.0, 2.0, 1.25)])  # 2s of picture plays as 2.5s
    assert warp.to_output(5.0) == 5.0
    assert warp.to_output(10.0) == 10.0
    assert warp.to_output(11.0) == pytest.approx(11.25)
    assert warp.to_output(12.0) == pytest.approx(12.5)
    assert warp.to_output(20.0) == pytest.approx(20.5)
    assert warp.to_source(11.25) == pytest.approx(11.0)
    assert warp.to_source(12.5) == pytest.approx(12.0)
    assert warp.to_source(20.5) == pytest.approx(20.0)
    assert warp.extra_seconds() == pytest.approx(0.5)


def test_segments_accumulate_and_round_trip():
    rng = random.Random(7)
    segments, t = [], 0.0
    for _ in range(120):
        t += rng.uniform(0.2, 3.0)
        length = rng.uniform(0.3, 4.0)
        segments.append(WarpSegment(t, length, 1.0 + rng.uniform(0.0, 0.111)))
        t += length
    rng.shuffle(segments)  # order of construction must not matter
    warp = TimeWarp(segments)
    assert warp.to_output(t + 10) == pytest.approx(t + 10 + warp.extra_seconds())
    previous = -1.0
    for _ in range(2000):
        source = rng.uniform(0.0, t + 10)
        out = warp.to_output(source)
        assert abs(warp.to_source(out) - source) < 1e-6
    for source in sorted(rng.uniform(0.0, t) for _ in range(500)):
        out = warp.to_output(source)
        assert out >= previous
        previous = out
    for s in segments:  # segment edges
        for edge in (s.start, s.start + s.length):
            assert abs(warp.to_source(warp.to_output(edge)) - edge) < 1e-6


def test_json_round_trip():
    warp = TimeWarp([WarpSegment(3.0, 1.5, 1.1), WarpSegment(1.0, 0.5, 1.05)])
    again = TimeWarp.from_json(warp.to_json())
    assert again.segments == warp.segments
    assert [s.start for s in again.segments] == [1.0, 3.0]


def test_setpts_chains_filters_latest_segments_first():
    warp = TimeWarp([WarpSegment(float(i * 2), 1.0, 1.1) for i in range(90)])
    graph = warp.setpts_filter(per=40)
    assert graph.startswith("settb=1/90000,setpts='")
    assert graph.count("setpts=") == 3  # 40 + 40 + 10 terms
    starts = [float(x) for x in re.findall(r"clip\(T-([\d.]+),", graph)]
    assert starts == sorted(starts, reverse=True)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_ffmpeg_frame_times_follow_the_warp():
    fps, duration = 30, 20.0
    rng = random.Random(3)
    segments = [WarpSegment(i * 0.3 + rng.uniform(0, 0.05), 0.2, 1.111) for i in range(60)]
    warp = TimeWarp(segments)
    run = subprocess.run(
        ["ffmpeg", "-v", "info", "-f", "lavfi", "-i", f"testsrc=size=64x36:rate={fps}:duration={duration}",
         "-vf", f"{warp.setpts_filter()},showinfo", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    times = [float(x) for x in re.findall(r"pts_time:([\d.]+)", run.stderr)]
    assert len(times) == int(fps * duration)
    for index in (0, len(times) // 2, len(times) - 1):
        assert abs(times[index] - warp.to_output(index / fps)) < 1.0 / fps

import json

from backend.app.core.config import Settings
from backend.app.domain.artifacts import Artifact, ArtifactKind
from backend.app.domain.jobs import JobInput
from backend.app.library import outputs
from backend.app.services.job_service import JobService


def _finished_job(tmp_path):
    settings = Settings(root_dir=tmp_path, _env_file=None)
    settings.ensure_directories()
    service = JobService(settings)
    manifest = service.create(JobInput(url="https://example.com/v"))
    job = manifest.job_id

    hardsub = settings.packages_dir / f"{job}.vi.display.hardsub.mp4"
    hardsub.write_bytes(b"video")
    voice = settings.tts_dir / f"{job}.voice.m4a"
    voice.write_bytes(b"voice")
    # the first 2 s are slowed to 1.5x: everything after starts 1 s later on the render
    (settings.tts_dir / f"{job}.tts.json").write_text(json.dumps({"time_warp": [{"start": 0, "length": 2, "factor": 1.5}]}))
    (settings.subtitles_dir / f"{job}.vi.display.json").write_text(json.dumps([{"start": 5.0, "end": 6.0, "text": "Xin chào"}]), encoding="utf-8")
    (settings.subtitles_dir / f"{job}.speech.tsv").write_text(
        "SUB_ID\tANCHOR_ID\tANCHOR_START\tANCHOR_END\tSOURCE_IDS\tTEXT\n0\t0\t5.000\t6.000\t0\tXin chào cả nhà.\n", encoding="utf-8")
    (settings.subtitles_dir / f"{job}.glossary.json").write_text(json.dumps([
        {"term_zh": "颜季明", "term_vi": "Nhan Quý Minh", "kind": "name", "note": "nam; anh; gộp 严继明"},
        {"term_zh": "长安", "term_vi": "Trường An", "kind": "place", "note": ""},
    ], ensure_ascii=False), encoding="utf-8")
    manifest.artifacts = [
        Artifact(kind=ArtifactKind.voice_track, path=str(voice.relative_to(settings.data_dir)), mime_type="audio/mp4"),
        Artifact(kind=ArtifactKind.hardsub_mp4, path=str(hardsub.relative_to(settings.data_dir)), mime_type="video/mp4"),
    ]
    return settings, manifest


def _info(**changes) -> outputs.EpisodeInfo:
    base = dict(owner_id="ep1", series_title="Nghiên Biên: Thanh Mai", label=outputs.episode_label("砚边青梅 第3集"),
                title="砚边青梅 第3集", title_vi="Thanh mai bên nghiên", source_url="https://example.com/v")
    return outputs.EpisodeInfo(**(base | changes))


def test_an_episode_gathers_into_series_and_episode_folders(tmp_path):
    settings, manifest = _finished_job(tmp_path)
    folder = outputs.gather(settings, manifest, _info())

    # ":" is not allowed in folder names on every OS
    assert folder == settings.data_dir / "outputs" / "Nghiên Biên Thanh Mai" / "Tập 03"
    # media is a hard link: no second copy of a 400 MB video
    assert (folder / "video.mp4").stat().st_ino == (settings.packages_dir / f"{manifest.job_id}.vi.display.hardsub.mp4").stat().st_ino
    assert (folder / "voice.m4a").read_bytes() == b"voice"
    # the subtitle follows the stretched render, not the source clock
    assert "00:00:06,000 --> 00:00:07,000" in (folder / "phu-de.vi.srt").read_text(encoding="utf-8")

    info = (folder / "thong-tin.md").read_text(encoding="utf-8")
    assert "- Tiêu đề tiếng Việt: Thanh mai bên nghiên" in info
    assert "- Nhan Quý Minh (颜季明): nam; anh" in info
    assert "gộp" not in info and "Trường An" not in info  # only characters, no translator notes
    assert "[00:06] Xin chào cả nhà." in info


def test_a_rerender_refreshes_the_same_folder(tmp_path):
    settings, manifest = _finished_job(tmp_path)
    first = outputs.gather(settings, manifest, _info())
    hardsub = settings.packages_dir / f"{manifest.job_id}.vi.display.hardsub.mp4"
    hardsub.unlink()
    hardsub.write_bytes(b"new render")  # the render writes a new file, not the old inode

    assert outputs.gather(settings, manifest, _info(title_vi="Tên đã đổi")) == first
    assert (first / "video.mp4").read_bytes() == b"new render"


def test_two_episodes_with_the_same_label_do_not_overwrite_each_other(tmp_path):
    settings, manifest = _finished_job(tmp_path)
    first = outputs.gather(settings, manifest, _info())
    second = outputs.gather(settings, manifest, _info(owner_id="ep2abcdef"))

    assert first != second
    assert second.name == "Tập 03 (ep2abc)"


def test_removing_a_dub_keeps_the_users_own_files(tmp_path):
    settings, manifest = _finished_job(tmp_path)
    folder = outputs.gather(settings, manifest, _info())
    (folder / "thumbnail-cua-toi.png").write_bytes(b"png")

    outputs.remove(settings, "Nghiên Biên: Thanh Mai", "ep1")

    assert sorted(p.name for p in folder.iterdir()) == ["thumbnail-cua-toi.png"]


def test_removing_the_only_dub_drops_the_emptied_folders(tmp_path):
    settings, manifest = _finished_job(tmp_path)
    outputs.gather(settings, manifest, _info())

    outputs.remove(settings, "Nghiên Biên: Thanh Mai", "ep1")

    assert not (settings.data_dir / "outputs" / "Nghiên Biên Thanh Mai").exists()


def test_a_job_without_a_rendered_video_gathers_nothing(tmp_path):
    settings, manifest = _finished_job(tmp_path)
    manifest.artifacts = [a for a in manifest.artifacts if a.kind != ArtifactKind.hardsub_mp4]

    assert outputs.gather_job(settings, manifest) is None
    assert not (settings.data_dir / "outputs").exists()


def test_a_video_outside_the_library_gathers_under_its_download_title(tmp_path):
    settings, manifest = _finished_job(tmp_path)
    manifest.metrics = {"download": {"title": "【1-2季】砚边青梅", "uploader": "不腻漫剧"}}

    folder = outputs.gather_job(settings, manifest)

    assert folder == settings.data_dir / "outputs" / "Video lẻ" / "【1-2季】砚边青梅"


def test_an_unrendered_dub_gathers_the_source_and_a_render_guide(tmp_path):
    settings, manifest = _finished_job(tmp_path)
    manifest.artifacts = [a for a in manifest.artifacts if a.kind != ArtifactKind.hardsub_mp4]
    (settings.videos_dir / f"{manifest.job_id}.mp4").write_bytes(b"source")

    folder = outputs.gather(settings, manifest, _info())

    assert (folder / "video-goc.mp4").read_bytes() == b"source"
    assert not (folder / "video.mp4").exists()
    info = (folder / "thong-tin.md").read_text(encoding="utf-8")
    assert "## Hướng dẫn render" in info
    assert "00:00–00:02: tốc độ 0.67x" in info  # where the partner must slow the picture

    # once rendered, the render replaces the source in the folder
    hardsub = settings.packages_dir / f"{manifest.job_id}.vi.display.hardsub.mp4"
    manifest.artifacts.append(Artifact(kind=ArtifactKind.hardsub_mp4, path=str(hardsub.relative_to(settings.data_dir)), mime_type="video/mp4"))
    outputs.gather(settings, manifest, _info())
    assert (folder / "video.mp4").exists() and not (folder / "video-goc.mp4").exists()
    assert "Hướng dẫn render" not in (folder / "thong-tin.md").read_text(encoding="utf-8")

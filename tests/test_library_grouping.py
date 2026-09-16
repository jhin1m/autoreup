from dataclasses import dataclass

from backend.app.library.grouping import cluster_into_series, episode_number


@dataclass
class _V:
    platform_video_id: str
    title: str


def test_episode_number_parses_arabic_and_chinese():
    assert episode_number("第7集 xxx") == 7
    assert episode_number("第二十三集") == 23
    assert episode_number("không có số") is None


def test_one_show_channel_survives_reuploads():
    """A creator re-posting old episodes AFTER new ones made the number 'reset' and
    one show shattered into several phantom series. Unique numbers => one show."""
    videos = [_V(str(100 + i), f"第{n}集") for i, n in enumerate([1, 2, 3, 4, 5, 6, 7, 8])]
    # re-uploads later (larger ids), numbers jump back — but stay unique
    videos += [_V("300", "第10集"), _V("301", "第9集")]
    groups = cluster_into_series(videos)
    assert len(groups) == 1
    nums = [episode_number(v.title) for v in groups[0].videos]
    assert nums == sorted(nums)  # ordered by episode, not upload


def test_interleaved_shows_still_split_on_reset():
    # show A: 1..3 then show B: 1..3 (duplicate numbers -> reset heuristic)
    videos = [_V(str(i), f"第{n}集 #showA") for i, n in enumerate([1, 2, 3])]
    videos += [_V(str(10 + i), f"第{n}集 #showB") for i, n in enumerate([1, 2, 3])]
    groups = cluster_into_series(videos)
    assert len(groups) == 2
    assert groups[0].title == "showA" and groups[1].title == "showB"


def test_group_name_from_first_descriptive_title():
    videos = [_V("1", "第1集 全球资源枯竭，高等文明奖励！"), _V("2", "第2集"), _V("3", "第3集")]
    groups = cluster_into_series(videos)
    assert len(groups) == 1
    assert groups[0].title == "全球资源枯竭"


def test_reconcile_unfinishes_a_dubbed_episode_when_its_job_runs_again(tmp_path):
    """A package re-render runs the same job again: the cached 'dubbed' state must go
    back to 'translating' or the UI shows nothing and never polls for the result."""
    import json
    from types import SimpleNamespace

    from backend.app.core.config import Settings
    from backend.app.domain.jobs import JobState
    from backend.app.library.service import LibraryService
    from backend.app.library.models import DubState, Episode

    settings = Settings(root_dir=tmp_path, _env_file=None)
    settings.ensure_directories()
    jobs = SimpleNamespace(get=lambda job_id: SimpleNamespace(state=JobState.running))
    service = LibraryService(settings, jobs)
    series = service.store.create_series("S")
    episode = Episode(
        id="ep1", series_id=series.id, platform="douyin", platform_video_id="v1",
        source_url="https://example.com/v", title="t", thumbnail_url=None, duration=10.0,
        uploader=None, discovered_at="2026-01-01T00:00:00Z",
    )
    service.store.upsert_episodes(series.id, [episode])
    service.store.set_episode_dub("ep1", "job1", DubState.dubbed)

    service._reconcile(series.id)
    assert service.store.list_episodes(series.id)[0].dub_state == DubState.translating

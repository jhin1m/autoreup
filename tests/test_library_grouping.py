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

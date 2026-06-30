"""Split a channel's mixed videos into series ("bộ").

A reup channel posts several shows interleaved; the reliable boundary is the episode
number ("第X集") resetting along the channel's UPLOAD order — a show runs 1..N, then the
next show starts again at 1. Hashtags alone fail (early episodes of a show often carry
only generic tags like #悬疑), so we cut on the number reset and *name* each group from
its dominant show hashtag.
"""

import re
from dataclasses import dataclass, field

# Generic tags that describe genre, not the show — never used as a series name.
_GENERIC_TAGS = {
    "原创动画", "动画", "动漫", "国漫", "悬疑", "推理", "悬疑推理", "推理悬疑", "烧脑",
    "反转", "短剧", "爽文", "解说", "故事", "剧情", "权谋", "古风", "热血", "情感",
}
_HASHTAG_RE = re.compile(r"#([^\s#]+)")
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text: str) -> int | None:
    """Parse a Chinese (一/二/十…) or Arabic episode number."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if not text:
        return None
    if text == "十":
        return 10
    if "十" in text:
        tens, _, ones = text.partition("十")
        t = _CN_DIGITS.get(tens, 1) if tens else 1
        o = _CN_DIGITS.get(ones, 0) if ones else 0
        return t * 10 + o
    total = 0
    for ch in text:
        if ch not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[ch]
    return total or None


def episode_number(title: str) -> int | None:
    match = re.search(r"第\s*([0-9零一二两三四五六七八九十]+)\s*集", title)
    return _cn_number(match.group(1)) if match else None


def show_tags(title: str) -> list[str]:
    return [t for t in _HASHTAG_RE.findall(title) if t not in _GENERIC_TAGS]


@dataclass
class Group:
    title: str
    videos: list = field(default_factory=list)   # SourceVideo, in episode order


def cluster_into_series(videos: list) -> list[Group]:
    """Group videos into series by episode-number reset along upload order.

    ``videos`` are SourceVideo-like (``.platform_video_id``, ``.title``). Numeric video
    ids increase with upload time, so sorting by id ascending recovers upload order.
    """
    ordered = sorted(videos, key=lambda v: _sort_key(v.platform_video_id))
    groups: list[list] = []
    last_num: int | None = None
    for video in ordered:
        num = episode_number(video.title)
        # Start a new group at the first video, or whenever the episode count resets
        # (num <= the previous number) — that's the seam between two shows.
        if not groups or (num is not None and last_num is not None and num <= last_num):
            groups.append([])
        groups[-1].append(video)
        if num is not None:
            last_num = num
    return [Group(title=_name_group(g), videos=g) for g in groups]


def _name_group(videos: list) -> str:
    """Name a group by its most common show hashtag; fall back to a cleaned title."""
    counts: dict[str, int] = {}
    for video in videos:
        for tag in show_tags(video.title):
            counts[tag] = counts.get(tag, 0) + 1
    if counts:
        return max(counts, key=lambda tag: (counts[tag], len(tag)))
    # No show tag anywhere — strip the "第X集：" prefix off the first title.
    first = videos[0].title if videos else "Bộ mới"
    return re.sub(r"^第\s*[0-9零一二两三四五六七八九十]+\s*集[：:、\s]*", "", first).split("#")[0].strip() or "Bộ mới"


def _sort_key(video_id: str):
    return (0, int(video_id)) if str(video_id).isdigit() else (1, str(video_id))

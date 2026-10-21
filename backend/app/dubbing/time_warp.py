"""Local video stretch: the map between SOURCE time and the rendered OUTPUT time.

A dub line that cannot fit before the next caption even at the voice's tempo
ceiling slows the picture down over that caption's window instead (factor > 1;
1.111 = the video plays at 0.9x). Everything in the pipeline stays in source time —
timings, the fit loop, subtitles, the editor — and only two places convert to the
output time: assembling the voice track and the hardsub render.

ffmpeg note (measured): one ``setpts`` expression with more than ~50 terms fails to
parse ("Cannot allocate memory"). The filter is therefore a chain of ``setpts``
filters of at most ``per`` terms, LATEST segments first — a later segment shifts
only frames after it, so an earlier segment's ``clip()`` term still sees its own
window untouched. ``settb=1/90000`` first: chaining at the source's 1/30 timebase
rounds every stage and drifted 0.21s over 400 segments (0.014s with the fine one).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class WarpSegment:
    start: float   # source seconds
    length: float  # source seconds
    factor: float  # > 1 = slowed (1.111 -> 0.9x)

    @property
    def extra(self) -> float:
        return self.length * (self.factor - 1.0)


@dataclass(slots=True)
class TimeWarp:
    """Non-overlapping stretch segments; no segment = identity."""

    segments: list[WarpSegment] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.segments = sorted(self.segments, key=lambda s: s.start)

    def __bool__(self) -> bool:
        return bool(self.segments)

    def to_output(self, t: float) -> float:
        return t + sum(s.extra if t >= s.start + s.length else (s.factor - 1.0) * (t - s.start)
                       for s in self.segments if t > s.start)

    def to_source(self, t: float) -> float:
        extra = 0.0
        for s in self.segments:
            out_start = s.start + extra
            if t <= out_start:
                break
            if t < out_start + s.length * s.factor:
                return s.start + (t - out_start) / s.factor
            extra += s.extra
        return t - extra

    def extra_seconds(self) -> float:
        return sum(s.extra for s in self.segments)

    def setpts_filter(self, per: int = 40) -> str:
        """``settb=1/90000,setpts=...,setpts=...`` (see module docstring); "" = identity."""
        if not self.segments:
            return ""
        latest_first = sorted(self.segments, key=lambda s: s.start, reverse=True)
        stages = []
        for lo in range(0, len(latest_first), max(1, per)):
            terms = "".join(
                f"+{s.factor - 1.0:.6f}*clip(T-{s.start:.6f},0,{s.length:.6f})"
                for s in latest_first[lo : lo + per]
            )
            stages.append(f"setpts='(T{terms})/TB'")  # quoted: clip() commas would split the graph
        return "settb=1/90000," + ",".join(stages)

    def to_json(self) -> list[dict]:
        return [{"start": round(s.start, 6), "length": round(s.length, 6), "factor": round(s.factor, 6)} for s in self.segments]

    @classmethod
    def from_json(cls, data: list[dict] | None) -> "TimeWarp":
        return cls([WarpSegment(float(d["start"]), float(d["length"]), float(d["factor"])) for d in data or []])

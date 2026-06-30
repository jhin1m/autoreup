import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from backend.app.services.subtitle_formats import cue_text


TAG_RE = re.compile(r"<[^>]+>")
BRACKET_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)|（[^）]*）")
TTS_PUNCT_RE = re.compile(r"[,.!?…;:，。！？；：、\"“”‘’']+")
SENTENCE_PUNCT = ".!?…;:，。！？；：,"


@dataclass(frozen=True)
class TtsBlock:
    block_id: str
    start: float
    end: float
    text: str
    source_cue_ids: tuple[int, ...] = ()

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["source_cue_ids"] = list(self.source_cue_ids)
        data["duration"] = round(self.duration, 3)
        return data


def clean_tts_text(text: str, text_mode: str = "natural") -> str:
    text = TAG_RE.sub(" ", str(text))
    text = BRACKET_RE.sub(" ", text)
    text = text.replace("\n", " ")
    if text_mode == "continuous":
        # No pauses at all.
        text = TTS_PUNCT_RE.sub(" ", text)
    else:
        # "natural": trust the translation's punctuation — in well-formed Vietnamese a
        # comma/period IS a natural reading pause. Turn EVERY clause/sentence boundary
        # into ONE uniform light comma-pause so the prosody is consistent (no boundary
        # runs on while another pauses). Dashes are not pauses -> space.
        text = re.sub(r"[—–]+", " ", text)
        text = re.sub(r"[,.!?…;:、，。！？；：]+", ", ", text)
        # A comma right before a short closing/tag word is an odd micro-pause in
        # speech (e.g. "có đúng, không" -> "có đúng không"); drop just that one.
        text = re.sub(r",\s+(không|chứ|nhé|nha|nhỉ|ạ|à|ư|hả|hử)\b", r" \1", text)
    text = " ".join(text.split())
    text = re.sub(r"(\s*[,，]\s*)+", ", ", text)  # collapse/normalize commas
    text = re.sub(r"\s+([,.!?…;:，。！？；：、])", r"\1", text)
    # Drop trailing punctuation: a phrase's end-pause is wasted (the scheduled gap
    # to the next anchor already provides it) and only overruns the window.
    return text.rstrip(".,!?…;:，。！？；：、 ").strip()


def build_tts_blocks(cues: list[dict], text_mode: str = "continuous") -> list[TtsBlock]:
    blocks: list[TtsBlock] = []
    current_key: str | None = None
    current: list[dict] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        start = min(float(cue["start"]) for cue in current)
        end = max(float(cue["end"]) for cue in current)
        text = clean_tts_text(" ".join(cue_text(cue) for cue in current), text_mode=text_mode)
        source_ids: list[int] = []
        for cue in current:
            for cue_id in cue.get("source_cue_ids") or []:
                if cue_id not in source_ids:
                    source_ids.append(cue_id)
        key = str(current[0].get("display_split_from") or current[0].get("span_id") or len(blocks))
        if text and end > start:
            blocks.append(TtsBlock(block_id=key, start=round(start, 3), end=round(end, 3), text=text, source_cue_ids=tuple(source_ids)))
        current = []

    for cue in sorted(cues, key=lambda item: (float(item.get("start", 0)), float(item.get("end", 0)))):
        split_key = cue.get("display_split_from")
        key = str(split_key) if split_key else f"cue:{cue.get('span_id') or len(blocks)}:{float(cue.get('start', 0)):.3f}"
        if current and key != current_key:
            flush()
        current_key = key
        current.append(cue)
    flush()
    return blocks


def group_tts_blocks(blocks: list[TtsBlock], max_gap: float = 0.75, max_duration: float = 12.0, max_chars: int = 220) -> list[TtsBlock]:
    if not blocks:
        return []
    if max_duration <= 0 or max_chars <= 0:
        return blocks

    grouped: list[TtsBlock] = []
    current: list[TtsBlock] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        source_ids: list[int] = []
        for block in current:
            for cue_id in block.source_cue_ids:
                if cue_id not in source_ids:
                    source_ids.append(cue_id)
        grouped.append(TtsBlock(
            block_id="-".join(block.block_id for block in current),
            start=current[0].start,
            end=current[-1].end,
            text=join_group_text([block.text for block in current]),
            source_cue_ids=tuple(source_ids),
        ))
        current = []

    for block in sorted(blocks, key=lambda item: (item.start, item.end)):
        if not current:
            current.append(block)
            continue
        gap = block.start - current[-1].end
        candidate_duration = block.end - current[0].start
        candidate_text = join_group_text([*(item.text for item in current), block.text])
        if gap <= max_gap and candidate_duration <= max_duration and len(candidate_text) <= max_chars:
            current.append(block)
        else:
            flush()
            current.append(block)
    flush()
    return grouped


def join_group_text(parts: list[str]) -> str:
    out = ""
    for raw in parts:
        text = " ".join(str(raw or "").split())
        if not text:
            continue
        if not out:
            out = text
            continue
        separator = " " if out[-1] in SENTENCE_PUNCT else ", "
        out = f"{out}{separator}{text}"
    return out.strip()


def to_provider_scale(value: float | int | None, default: float = 1.0) -> int:
    numeric = float(default if value is None else value)
    return max(1, min(20, int(round(numeric * 10))))


def cache_key(provider: str, speaker: str, text: str, speed: float = 1.0, pitch: float = 1.0, volume: float = 1.0, text_mode: str = "continuous") -> str:
    payload = json.dumps(
        {
            "provider": provider.rstrip("/"),
            "speaker": speaker,
            "text": text,
            "speed": round(float(speed), 4),
            "pitch": round(float(pitch), 4),
            "volume": round(float(volume), 4),
            "text_mode": text_mode,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class HttpTtsProvider:
    def __init__(self, base_url: str, speaker: str, speed: float = 1.0, pitch: float = 1.0, volume: float = 1.0, timeout: float = 30.0, attempts: int = 3):
        self.base_url = base_url.rstrip("/")
        self.speaker = speaker
        self.speed = speed
        self.pitch = pitch
        self.volume = volume
        self.timeout = timeout
        self.attempts = attempts

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v2/synthesize"

    def synthesize(self, text: str) -> bytes:
        body = json.dumps(
            {
                "text": text,
                "type": 0,
                "speaker": self.speaker,
                "pitch": to_provider_scale(self.pitch),
                "speed": to_provider_scale(self.speed),
                "volume": to_provider_scale(self.volume),
                "method": "buffer",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "audio/mpeg, audio/*, */*"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                    if not payload:
                        raise RuntimeError("TTS provider returned empty audio")
                    return payload
            except urllib.error.HTTPError as exc:
                if exc.code < 500 and exc.code != 429:
                    raise RuntimeError(f"TTS provider rejected request: HTTP {exc.code}") from exc
                last_error = exc
            except (TimeoutError, urllib.error.URLError, RuntimeError) as exc:
                last_error = exc
            if attempt < self.attempts:
                time.sleep(0.7 * (attempt + 1))
        raise RuntimeError(f"TTS provider failed after {self.attempts} attempts: {last_error}")


def raw_audio_path(cache_dir: Path, provider_url: str, speaker: str, block: TtsBlock, speed: float, pitch: float, volume: float, text_mode: str) -> Path:
    key = cache_key(provider_url, speaker, block.text, speed=speed, pitch=pitch, volume=volume, text_mode=text_mode)
    return cache_dir / "raw" / f"{key}.mp3"

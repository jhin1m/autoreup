"""L4 — unit translation (v3): sentences in, sentences out.

Design (evidence-driven, see docs/TRANSLATE-V3.md):

* units are semantic (built by ``units.build_units`` from the ASR rail) — NOT timing
  anchors and NOT caption rows;
* no character budget in the prompt: wording quality comes first, timing is handled
  later by the deterministic timeline (run-level tempo + breath);
* batches run sequentially with rolling context (previous source + previous
  Vietnamese) so pronouns and tone carry across the video;
* after each batch a single gated EDIT pass makes the prose engaging; the words then
  freeze — reflow/render never rewrite them again;
* rows are split ONLY on explicit-turn markers (``⟂``); caption splitting belongs to
  the display stage.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.agents import AgentConfig, complete

from .edit import EditConfig, edit_texts
from .glossary import format_glossary
from .models import Anchor, GlossaryEntry, SpeechRow
from .prompts import TRANSLATE_SYSTEM, TRANSLATE_USER
from .text import has_han, sanitize_caption, split_text_for_explicit_turns_only
from .tsv_io import parse_tsv_rows

_CONTEXT_SOURCE_CHARS = 200
_CONTEXT_VI_CHARS = 240


class TranslateError(RuntimeError):
    pass


@dataclass(slots=True)
class TranslateConfig:
    notes: str = ""           # per-video context (cast/gender/register) injected into the prompt
    batch_units: int = 20     # units per translation call
    edit: bool = True         # one gated creative pass per batch (L4.5)


def _parse_output(raw: str, valid_ids: set[int]) -> dict[int, str]:
    texts: dict[int, str] = {}
    for cells in parse_tsv_rows(raw, expected_cols=2):
        try:
            unit_id = int(cells[0].strip())
        except ValueError:
            continue
        if unit_id not in valid_ids:
            continue
        text = sanitize_caption(cells[1])
        if text:
            texts[unit_id] = text
    return texts


def _validate(texts: dict[int, str], units: list[Anchor]) -> None:
    for unit in units:
        text = texts.get(unit.id)
        if not text:
            raise TranslateError(f"unit {unit.id} has no translation")
        if has_han(text):
            raise TranslateError(f"unit {unit.id} translation still contains Han characters")


def translate_batch(
    units: list[Anchor],
    glossary: list[GlossaryEntry],
    prev_source: str,
    prev_vi: str,
    llm: AgentConfig,
    config: TranslateConfig,
) -> dict[int, str]:
    def attempt(batch: list[Anchor]) -> dict[int, str]:
        user = TRANSLATE_USER.format(
            glossary=format_glossary(glossary),
            notes=config.notes or "(không có)",
            prev_source=prev_source or "(đầu video)",
            prev_vi=prev_vi or "(chưa có)",
            units="\n".join(f"{a.id}\t{a.source_zh}" for a in batch),
        )
        raw = complete(TRANSLATE_SYSTEM, user, llm)
        return _parse_output(raw, {a.id for a in batch})

    texts = attempt(units)
    # per-line flake retry (skipped line / Han leak), not a whole-batch failure
    bad = [a for a in units if not texts.get(a.id) or has_han(texts[a.id])]
    if bad:
        retried = attempt(bad)
        texts.update({uid: t for uid, t in retried.items() if t and not has_han(t)})
    _validate(texts, units)

    if config.edit:
        texts, _accepted = edit_texts(texts, units, glossary, llm)
        _validate(texts, units)
    return texts


def _rows_from_texts(texts: dict[int, str], units: list[Anchor], start_sub_id: int) -> list[SpeechRow]:
    rows: list[SpeechRow] = []
    sub_id = start_sub_id
    for unit in units:
        parts = split_text_for_explicit_turns_only(texts[unit.id]) or [texts[unit.id]]
        for part in parts:
            rows.append(
                SpeechRow(
                    sub_id=sub_id,
                    anchor_id=unit.id,
                    anchor_start=unit.start,
                    anchor_end=unit.end,
                    source_ids=unit.source_ids,
                    text=part,
                )
            )
            sub_id += 1
    return rows


def translate(
    units: list[Anchor],
    glossary: list[GlossaryEntry],
    llm: AgentConfig,
    config: TranslateConfig | None = None,
) -> list[SpeechRow]:
    config = config or TranslateConfig()
    if not units:
        return []

    texts: dict[int, str] = {}
    prev_source = ""
    prev_vi = ""
    for start in range(0, len(units), max(1, config.batch_units)):
        batch = units[start : start + config.batch_units]
        batch_texts = translate_batch(batch, glossary, prev_source, prev_vi, llm, config)
        texts.update(batch_texts)
        prev_source = "".join(a.source_zh for a in batch)[-_CONTEXT_SOURCE_CHARS:]
        prev_vi = " ".join(batch_texts[a.id] for a in batch if batch_texts.get(a.id))[-_CONTEXT_VI_CHARS:]

    return _rows_from_texts(texts, units, start_sub_id=0)

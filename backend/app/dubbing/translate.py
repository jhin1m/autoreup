"""L4 — unit translation as ONE workspace agent run (F1 + LLM fundamentals).

An LLM translates best with the whole document in front of it, on units that carry a
complete thought, writing artifacts to disk it can re-read and fix. So this stage is
not a series of one-shot prompts: it prepares a workspace (``SKILL.md`` + ``input/``),
lets the agent run its full workflow there — read all → draft → self-review → final —
and then validates the FILES on disk. Model prose is never parsed for content.

The code still owns safety: every line the agent changed between draft and final goes
through ``gate_patch`` (numbers, negations, glossary terms, length), so a bad edit can
only ever be rejected, never smuggled in.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.agents import AgentConfig
from backend.app.agents.workspace import WorkspaceRun, run_agent_workspace

from .edit import EditConfig, gate_patch
from .models import Anchor, GlossaryEntry, SpeechRow
from .text import has_han, has_kana, sanitize_caption

_SKILL_PATH = Path(__file__).with_name("skills") / "translate" / "SKILL.md"
_AGENT_PROMPT = "Đọc SKILL.md và làm đầy đủ workflow trong đó với input/units.tsv."


class TranslateError(RuntimeError):
    pass


@dataclass(slots=True)
class TranslateConfig:
    notes: str = ""            # per-video context, written to input/notes.md
    timeout: float = 1200.0    # one workspace run may loop for many minutes
    retries: int = 2           # follow-up turns for ids the agent left missing


@dataclass(slots=True)
class TranslateResult:
    rows: list[SpeechRow]
    runs: list[WorkspaceRun] = field(default_factory=list)


def _parse_table(path: Path) -> dict[int, str]:
    """Read ``ID<TAB>TEXT`` rows from a file the agent wrote (tolerates fences/quotes)."""
    texts: dict[int, str] = {}
    if not path.exists():
        return texts
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip().strip("|").strip()
        if not line or line.startswith(("```", "#")):
            continue
        cells = [cell.strip() for cell in line.split("\t")]
        if len(cells) < 2:
            continue
        key = cells[0].lstrip("*").strip()
        if not key.isdigit():
            continue
        text = sanitize_caption(cells[1])
        if text:
            texts[int(key)] = text
    return texts


def _prepare_workspace(workspace: Path, units: list[Anchor], glossary: list[GlossaryEntry], notes: str) -> None:
    (workspace / "input").mkdir(parents=True, exist_ok=True)
    out = workspace / "out"
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.tsv"):
        stale.unlink()
    shutil.copyfile(_SKILL_PATH, workspace / "SKILL.md")
    (workspace / "input" / "units.tsv").write_text(
        "\n".join(f"{u.id}\t{u.start:.2f}\t{u.end:.2f}\t{u.source_zh}" for u in units) + "\n", encoding="utf-8"
    )
    (workspace / "input" / "glossary.tsv").write_text(
        "\n".join(f"{g.term_zh}\t{g.term_vi}\t{g.kind}\t{g.note}" for g in glossary) + "\n", encoding="utf-8"
    )
    (workspace / "input" / "notes.md").write_text(notes or "(không có bối cảnh bổ sung)\n", encoding="utf-8")


def _collect(workspace: Path, units: list[Anchor], glossary: list[GlossaryEntry]) -> dict[int, str]:
    """Draft -> final, with every edit gated (unsafe edits fall back to the draft)."""
    draft = _parse_table(workspace / "out" / "draft.tsv")
    final = _parse_table(workspace / "out" / "final.tsv")
    texts: dict[int, str] = {}
    for unit in units:
        candidate = final.get(unit.id) or draft.get(unit.id)
        if candidate is None:
            continue
        base = draft.get(unit.id)
        if base and base != candidate:
            gated = gate_patch(base, candidate, glossary, EditConfig())
            candidate = gated or base
        texts[unit.id] = candidate
    return texts


def _unusable(texts: dict[int, str], units: list[Anchor]) -> list[int]:
    return [
        unit.id
        for unit in units
        if not (text := texts.get(unit.id)) or has_han(text) or has_kana(text)
    ]


def translate(
    units: list[Anchor],
    glossary: list[GlossaryEntry],
    llm: AgentConfig,
    workspace: Path,
    config: TranslateConfig | None = None,
) -> TranslateResult:
    """Run the translate skill in ``workspace``; return one row per unit."""
    config = config or TranslateConfig()
    if not units:
        return TranslateResult(rows=[])
    workspace = Path(workspace)
    _prepare_workspace(workspace, units, glossary, config.notes)

    runs = [run_agent_workspace(workspace, _AGENT_PROMPT, llm, timeout=config.timeout)]
    texts = _collect(workspace, units, glossary)
    missing = _unusable(texts, units)
    attempt = 0
    while missing and attempt < max(0, config.retries):
        attempt += 1
        prompt = (
            f"out/final.tsv còn thiếu/sai {len(missing)} ID: {', '.join(str(i) for i in missing[:40])}. "
            "Bổ sung và sửa đúng định dạng ID<TAB>TEXT (tiếng Việt, KHÔNG Hán tự, không rỗng), "
            "kiểm tra ĐỦ mọi ID trong input/units.tsv rồi ghi lại out/final.tsv."
        )
        runs.append(run_agent_workspace(workspace, prompt, llm, timeout=config.timeout / 2, resume=True))
        texts = _collect(workspace, units, glossary)
        missing = _unusable(texts, units)
    if missing:
        raise TranslateError(f"workspace run left {len(missing)} unit(s) untranslated: {missing[:10]}")

    rows = [
        SpeechRow(
            sub_id=index,
            anchor_id=unit.id,
            anchor_start=unit.start,
            anchor_end=unit.end,
            source_ids=unit.source_ids,
            text=texts[unit.id],
        )
        for index, unit in enumerate(units)
    ]
    return TranslateResult(rows=rows, runs=runs)

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

import hashlib
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from backend.app.agents import AgentConfig
from backend.app.agents.quota import gemini_headroom
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
    # A session (one agy run) is bounded by the CLI's print timeout AND by what the
    # agent can actually finish: measured, a 190-unit session = 6m17s, while a 404-unit
    # session produced ~40 units and then failed the stage. So: budget each session
    # well under the timeout, run sessions in parallel, and BISECT a session that
    # times out or comes back short instead of retrying it at the same size.
    timeout: float = 600.0     # wall-clock budget for ONE session
    retries: int = 1           # follow-up turns inside a session before bisecting
    part_units: int = 100      # measured capacity with ~2x headroom
    min_part_units: int = 25   # a "part" smaller than this is a unit-level retry
    max_split_depth: int = 3   # bisections per part (effective floor: 100/2^3 units)
    max_parallel: int = 4      # quota-aware cap (see _parallelism)


@dataclass(slots=True)
class TranslateResult:
    rows: list[SpeechRow]
    runs: list[WorkspaceRun] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)


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


def _workspace_matches(workspace: Path, digest: str) -> bool:
    stamp = workspace / "input" / ".content"
    return stamp.exists() and stamp.read_text(encoding="utf-8").strip() == digest


def _prepare_workspace(
    workspace: Path, units: list[Anchor], glossary: list[GlossaryEntry], notes: str, marked: set[int]
) -> bool:
    """(Re)build the workspace; returns True when a previous run's artifacts survive.

    The agent writes its own helpers and partial drafts here (observed: it split a
    too-big job itself with part1.py..part5.py). Those are progress, not garbage, so
    the workspace is CONTENT-ADDRESSED: same inputs -> keep out/ and let the next
    session continue (``--continue``); different inputs -> rebuild from scratch."""
    units_text = "\n".join(
        f"{'*' if u.id in marked else ' '}{u.id}\t{u.start:.2f}\t{u.end:.2f}\t{u.source_zh}" for u in units
    ) + "\n"
    digest = hashlib.sha256(units_text.encode("utf-8")).hexdigest()[:16]
    keep = _workspace_matches(workspace, digest)
    if workspace.exists() and not keep:
        shutil.rmtree(workspace)
    (workspace / "input").mkdir(parents=True, exist_ok=True)
    out = workspace / "out"
    out.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_SKILL_PATH, workspace / "SKILL.md")
    (workspace / "input" / ".content").write_text(digest, encoding="utf-8")
    (workspace / "input" / "units.tsv").write_text(units_text, encoding="utf-8")
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


def _run_part(
    part: list[Anchor],
    all_units: list[Anchor],
    glossary: list[GlossaryEntry],
    llm: AgentConfig,
    workspace: Path,
    config: TranslateConfig,
) -> tuple[dict[int, str], list[WorkspaceRun], list[int]]:
    """One session (plus its bounded follow-up turns) for one part."""
    marked = {unit.id for unit in part}
    keep = _prepare_workspace(workspace, all_units, glossary, config.notes, marked)
    resume = keep and (workspace / "out" / "draft.tsv").exists()
    if resume:
        runs = [run_agent_workspace(workspace, _AGENT_PROMPT, llm, timeout=config.timeout, resume=True)]
    else:
        runs = [run_agent_workspace(workspace, _AGENT_PROMPT, llm, timeout=config.timeout)]
    texts = _collect(workspace, part, glossary)
    missing = _unusable(texts, part)
    attempt = 0
    while missing and attempt < max(0, config.retries):
        attempt += 1
        prompt = (
            f"out/final.tsv còn thiếu/sai {len(missing)} ID: {', '.join(str(i) for i in missing[:40])}. "
            "Bổ sung và sửa đúng định dạng ID<TAB>TEXT (tiếng Việt, KHÔNG Hán tự, không rỗng), "
            "kiểm tra ĐỦ mọi ID có dấu * trong input/units.tsv rồi ghi lại out/final.tsv."
        )
        runs.append(run_agent_workspace(workspace, prompt, llm, timeout=config.timeout / 2, resume=True))
        texts = _collect(workspace, part, glossary)
        missing = _unusable(texts, part)
    return texts, runs, missing


def _parallelism(config: TranslateConfig, llm: AgentConfig) -> int:
    """How many sessions may run at once: part count, capped by measured quota headroom.

    Every agent call draws from the same 5-hour pool as the fit loop; when the oracle
    says the window is nearly spent, serialize instead of collecting 429s."""
    headroom = gemini_headroom(llm.bin)
    if headroom is None:
        return config.max_parallel
    if headroom < 0.3:
        return 1
    return max(1, config.max_parallel)


def _session(
    part: list[Anchor],
    all_units: list[Anchor],
    glossary: list[GlossaryEntry],
    llm: AgentConfig,
    workspace: Path,
    config: TranslateConfig,
    depth: int = 0,
) -> tuple[dict[int, str], list[WorkspaceRun], list[int], list[str]]:
    """Run one part; a session that comes back short is BISECTED, never retried as-is."""
    texts, runs, missing = _run_part(part, all_units, glossary, llm, workspace, config)
    logs: list[str] = []
    if missing and len(part) > config.min_part_units and depth < config.max_split_depth:
        mid = len(part) // 2
        logs.append(
            f"translate: session left {len(missing)}/{len(part)} units at depth {depth} -> "
            f"bisecting into {mid}+{len(part) - mid}"
        )
        left_texts, left_runs, left_missing, left_logs = _session(
            part[:mid], all_units, glossary, llm, workspace / "a", config, depth + 1
        )
        right_texts, right_runs, right_missing, right_logs = _session(
            part[mid:], all_units, glossary, llm, workspace / "b", config, depth + 1
        )
        texts.update(left_texts)
        texts.update(right_texts)
        runs += left_runs + right_runs
        missing = left_missing + right_missing
        logs += left_logs + right_logs
    return texts, runs, missing, logs


def translate(
    units: list[Anchor],
    glossary: list[GlossaryEntry],
    llm: AgentConfig,
    workspace: Path,
    config: TranslateConfig | None = None,
) -> TranslateResult:
    """Translate in quota-bounded parallel sessions, each seeing the whole transcript."""
    config = config or TranslateConfig()
    if not units:
        return TranslateResult(rows=[])
    workspace = Path(workspace)
    size = max(1, config.part_units)
    parts = [units[i : i + size] for i in range(0, len(units), size)]

    outcomes: list[tuple[dict[int, str], list[WorkspaceRun], list[int], list[str]]] = []
    if len(parts) == 1:
        # short video: one session at the workspace root, no quota oracle needed
        parallel = 1
        outcomes.append(_session(parts[0], units, glossary, llm, workspace, config))
    else:
        parallel = min(len(parts), _parallelism(config, llm))
        if parallel <= 1:
            for index, part in enumerate(parts):
                outcomes.append(_session(part, units, glossary, llm, workspace / f"part{index}", config))
        else:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                futures = [
                    pool.submit(_session, part, units, glossary, llm, workspace / f"part{index}", config)
                    for index, part in enumerate(parts)
                ]
                outcomes = [future.result() for future in futures]

    texts: dict[int, str] = {}
    runs: list[WorkspaceRun] = []
    missing: list[int] = []
    logs: list[str] = [f"translate: {len(parts)} part(s), {parallel} parallel, {config.part_units} units/session"]
    for part_texts, part_runs, part_missing, part_logs in outcomes:
        texts.update(part_texts)
        runs += part_runs
        missing += part_missing
        logs += part_logs
    if missing:
        raise TranslateError(f"workspace sessions left {len(missing)} unit(s) untranslated: {missing[:10]}")

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
    return TranslateResult(rows=rows, runs=runs, logs=logs)

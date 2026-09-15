import backend.app.dubbing.edit as edit_mod
import backend.app.dubbing.glossary as glossary_mod
import backend.app.dubbing.translate as translate_mod
from backend.app.dubbing.glossary import extract_glossary, format_glossary
from backend.app.agents import AgentConfig
from backend.app.dubbing.tsv_io import parse_tsv_rows
from backend.app.dubbing.models import Anchor, GlossaryEntry, SpeechRow
from backend.app.dubbing.units import UnitConfig, build_units
from backend.app.dubbing.models import AsrSegment
from backend.app.dubbing.translate import TranslateConfig, translate

LLM = AgentConfig()


def test_parse_tsv_rows_strips_fences_and_short_rows():
    raw = "```\n0\tXin chào\n1\tTạm biệt\nbroken\n```"
    rows = parse_tsv_rows(raw, expected_cols=2)
    assert rows == [["0", "Xin chào"], ["1", "Tạm biệt"]]


def test_parse_tsv_rows_joins_extra_columns():
    rows = parse_tsv_rows("0\ta\tb\tc", expected_cols=2)
    assert rows == [["0", "a\tb\tc"]]


def test_extract_glossary_parses_and_dedupes(monkeypatch):
    out = "罗瑞\tLa Thụy\tname\tnhân vật chính\n警察\tcảnh sát\tterm\t\n罗瑞\tLa Thụy\tname\tdup"
    monkeypatch.setattr(glossary_mod, "complete", lambda s, u, c: out)
    anchors = [Anchor(0, 0, 1, "0-0", "罗瑞是警察")]
    entries = extract_glossary(anchors, LLM, genre="hình sự")
    assert [e.term_zh for e in entries] == ["罗瑞", "警察"]
    assert "La Thụy" in format_glossary(entries)


def _rail_and_units():
    # 6 micro-segments -> 3 semantic units (2 segments each) via the v3 builder
    segs = [AsrSegment(i, i * 1.0, i * 1.0 + 0.9, f"字{i}") for i in range(6)]
    units = build_units(segs, UnitConfig(max_chars=4, min_chars=1))
    anchors = [Anchor(id=u.id, start=u.start, end=u.end, source_ids=u.segment_ids, source_zh=u.source_zh) for u in units]
    return segs, anchors


def test_translate_covers_every_caption_with_rail_windows(monkeypatch):
    segments, anchors = _rail_and_units()

    def fake(system, user, config):
        # one Vietnamese line per caption id present in the prompt
        return "\n".join(f"{s.id}\tLời {s.id}" for s in segments if f"{s.id}\t" in user)

    monkeypatch.setattr(translate_mod, "complete", fake)
    monkeypatch.setattr(edit_mod, "complete", fake)
    rows = translate(anchors, segments, [], LLM, TranslateConfig())
    # one row per caption, in rail order, each with the caption's own window
    assert [r.source_ids for r in rows] == [f"{s.id}-{s.id}" for s in segments]
    assert [r.anchor_start for r in rows] == [s.start for s in segments]
    assert [r.anchor_end for r in rows] == [s.end for s in segments]
    assert [r.sub_id for r in rows] == list(range(len(rows)))
    assert {r.anchor_id for r in rows} == {a.id for a in anchors}


def test_translate_raises_when_a_caption_has_no_line(monkeypatch):
    segments, anchors = _rail_and_units()
    # answers only the FIRST caption of each unit -> the rest are missing
    monkeypatch.setattr(translate_mod, "complete", lambda s, u, c: "0\tLời 0\n2\tLời 2\n4\tLời 4")
    monkeypatch.setattr(edit_mod, "complete", lambda s, u, c: "")
    try:
        translate(anchors, segments, [], LLM, TranslateConfig())
        assert False, "expected TranslateError"
    except translate_mod.TranslateError:
        pass


def test_translate_raises_on_han_leak(monkeypatch):
    segments, anchors = _rail_and_units()
    monkeypatch.setattr(translate_mod, "complete", lambda s, u, c: "\n".join(f"{s.id}\t还有中文" for s in segments))
    monkeypatch.setattr(edit_mod, "complete", lambda s, u, c: "")
    try:
        translate(anchors, segments, [], LLM, TranslateConfig())
        assert False, "expected TranslateError"
    except translate_mod.TranslateError:
        pass

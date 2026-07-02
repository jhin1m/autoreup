import backend.app.dubbing.glossary as glossary_mod
import backend.app.dubbing.translate as translate_mod
from backend.app.dubbing.glossary import extract_glossary, format_glossary
from backend.app.dubbing.llm import LLMConfig, parse_tsv_rows
from backend.app.dubbing.models import Anchor, GlossaryEntry, SpeechRow
from backend.app.dubbing.rail import AnchorConfig, build_anchors
from backend.app.dubbing.models import AsrSegment
from backend.app.dubbing.chunking import ChunkConfig, build_chunks, flat_scene_map
from backend.app.dubbing.translate import TranslateConfig, translate

LLM = LLMConfig()


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


def _anchors():
    segs = [AsrSegment(i, i * 1.0, i * 1.0 + 0.9, f"字{i}") for i in range(6)]
    return build_anchors(segs, AnchorConfig(max_anchor_segments=2, max_anchor_duration=999, max_anchor_source_chars=999))


def test_translate_covers_all_anchors_and_splits(monkeypatch):
    anchors = _anchors()  # 3 anchors
    scenes = flat_scene_map(anchors, ChunkConfig(flat_scene_anchors=2))
    chunks = build_chunks(anchors, scenes, ChunkConfig(target_duration=2, min_duration=1, max_duration=4))

    def fake(system, user, config):
        # one line per anchor id present in the prompt; anchor 1 has an explicit turn
        lines = []
        for a in anchors:
            if str(a.id) in user:
                text = "Ngươi là ai ⟂ Ta là ai" if a.id == 1 else f"Lời thoại {a.id}"
                lines.append(f"{a.id}\t{text}")
        lines.append("MEMORY\tnhanvat\tLa Thụy")
        return "\n".join(lines)

    monkeypatch.setattr(translate_mod, "complete", fake)
    rows = translate(anchors, chunks, [], LLM, TranslateConfig(max_caption_chars=40))
    covered = {r.anchor_id for r in rows}
    assert covered == {a.id for a in anchors}
    # explicit turn split produced two cues for anchor 1
    assert sum(1 for r in rows if r.anchor_id == 1) == 2
    # sub_ids are sequential
    assert [r.sub_id for r in rows] == list(range(len(rows)))


def test_translate_raises_on_han_leak(monkeypatch):
    anchors = _anchors()
    chunks = build_chunks(anchors, flat_scene_map(anchors), ChunkConfig(max_duration=999))
    monkeypatch.setattr(translate_mod, "complete", lambda s, u, c: "\n".join(f"{a.id}\t还有中文" for a in anchors))
    try:
        translate(anchors, chunks, [], LLM)
        assert False, "expected TranslateError"
    except translate_mod.TranslateError:
        pass

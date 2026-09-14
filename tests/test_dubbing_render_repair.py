import backend.app.dubbing.repair as repair_mod
from backend.app.dubbing.display import build_timed_units
from backend.app.agents import AgentConfig
from backend.app.dubbing.models import Anchor, SpeechRow
from backend.app.dubbing.render import RenderConfig, _blocks_from_units
from backend.app.dubbing.repair import RepairConfig, compute_targets, repair

LLM = AgentConfig()


def test_blocks_from_units_merge_rows_and_strip_turn_markers():
    anchors = [Anchor(0, 0.0, 3.0, "0-1", "源"), Anchor(1, 3.0, 6.0, "2-3", "源2")]
    rows = [
        SpeechRow(0, 0, 0.0, 3.0, "0-1", "Câu một ⟂ Câu hai"),
        SpeechRow(1, 0, 0.0, 3.0, "0-1", "Câu ba"),
        SpeechRow(2, 1, 3.0, 6.0, "2-3", "Anchor sau"),
    ]
    units = build_timed_units(rows, 0.4, anchors)
    blocks = _blocks_from_units(units, text_mode="continuous")
    # rows without sentence punctuation merge into one unit per anchor;
    # block_id keeps the anchor id so timings aggregate for the repair loop
    assert [b.block_id for b in blocks] == ["0", "1"]
    assert "⟂" not in blocks[0].text
    assert "Câu một" in blocks[0].text and "Câu ba" in blocks[0].text
    assert blocks[0].start == 0.0 and blocks[0].end == 3.0


def test_compute_targets_shrinks_overbudget_anchor():
    rows = [SpeechRow(0, 5, 0.0, 2.0, "x", "Một câu khá dài cần rút gọn lại nhiều")]
    grouped = {5: rows}
    # anchor 5 overran: window 2.0s but tts took 3.0s
    targets = compute_targets(grouped, {5: (2.0, 3.0)}, RepairConfig(target_scale=0.9))
    assert len(targets) == 1
    current_chars = len(rows[0].text)
    # target is proportionally shorter than current
    assert targets[0].target_chars < current_chars


def test_repair_accepts_shorter_rewrite_rejects_longer(monkeypatch):
    anchor = Anchor(5, 0.0, 2.0, "10-11", "源")
    rows = [SpeechRow(0, 5, 0.0, 2.0, "10-11", "Một câu rất dài lê thê cần được rút gọn đi nhiều lắm")]

    # rewrite is shorter -> accepted
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "5\tCâu ngắn")
    out, n = repair(rows, [anchor], [], {5: (2.0, 3.2)}, LLM, RepairConfig())
    assert n == 1
    assert out[0].text == "Câu ngắn"
    assert [r.sub_id for r in out] == list(range(len(out)))

    # rewrite is not shorter -> rejected, original kept
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "5\tMột câu còn dài hơn cả bản gốc lê thê dài dòng thêm nữa")
    out, n = repair(rows, [anchor], [], {5: (2.0, 3.2)}, LLM, RepairConfig())
    assert n == 0
    assert out[0].text.startswith("Một câu rất dài")


def test_repair_rejects_han(monkeypatch):
    anchor = Anchor(5, 0.0, 2.0, "10-11", "源")
    rows = [SpeechRow(0, 5, 0.0, 2.0, "10-11", "Một câu dài cần rút gọn")]
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "5\t还有中文")
    out, n = repair(rows, [anchor], [], {5: (2.0, 3.0)}, LLM, RepairConfig())
    assert n == 0


def test_render_config_defaults():
    cfg = RenderConfig(provider_url="http://x")
    assert cfg.speaker == "BV421_vivn_streaming"
    assert cfg.repair_over_ms == 300


def test_expand_completes_underfull_keeps_complete(monkeypatch):
    from backend.app.dubbing.repair import expand

    anchor = Anchor(5, 0.0, 6.0, "10-13", "源")
    rows = [SpeechRow(0, 5, 0.0, 6.0, "10-13", "Lời ngắn")]

    # LLM restores dropped content (much longer) -> accepted
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "5\tLời đầy đủ hơn nhiều với toàn bộ ý của câu gốc được khôi phục lại")
    out, n = expand(rows, [anchor], [], {5: (6.0, 2.0)}, LLM, RepairConfig())
    assert n == 1
    assert "đầy đủ" in out[0].text

    # LLM judges it complete and returns roughly the same -> kept unchanged
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "5\tLời ngắn")
    out, n = expand(rows, [anchor], [], {5: (6.0, 2.0)}, LLM, RepairConfig())
    assert n == 0
    assert out[0].text == "Lời ngắn"

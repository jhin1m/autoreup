import backend.app.dubbing.repair as repair_mod
from backend.app.agents import AgentConfig
from backend.app.dubbing.models import SpeechRow
from backend.app.dubbing.render import RenderConfig, _blocks_from_rows
from backend.app.dubbing.repair import RepairConfig, compute_targets, repair

LLM = AgentConfig()


def test_blocks_from_rows_is_one_clip_per_caption():
    rows = [
        SpeechRow(0, 0, 0.0, 3.0, "0-0", "Câu một ⟂ Câu hai"),
        SpeechRow(1, 1, 3.0, 6.0, "1-1", "Câu ba"),
    ]
    blocks, usable = _blocks_from_rows(rows, text_mode="continuous")
    # one block per row; the turn marker is stripped but the line stays whole
    assert [b.block_id for b in blocks] == ["0", "1"]
    assert "⟂" not in blocks[0].text
    assert "Câu một" in blocks[0].text and "Câu hai" in blocks[0].text
    assert blocks[0].start == 0.0 and blocks[0].end == 3.0
    assert [r.sub_id for r in usable] == [0, 1]


def test_compute_targets_shrinks_overbudget_row():
    row = SpeechRow(0, 5, 0.0, 2.0, "10-10", "Một câu khá dài cần rút gọn lại nhiều")
    # the free time is 2.0s but the measured voice took 3.0s
    targets = compute_targets({0: row}, {0: (2.0, 3.0)}, RepairConfig(target_scale=0.9))
    assert len(targets) == 1
    assert targets[0].sub_id == 0
    assert targets[0].target_chars < len(row.text)


def test_repair_accepts_shorter_rewrite_rejects_longer(monkeypatch):
    rows = [SpeechRow(0, 5, 0.0, 2.0, "10-10", "Một câu rất dài lê thê cần được rút gọn đi nhiều lắm")]

    # rewrite is shorter -> accepted
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\tCâu ngắn")
    out, n = repair(rows, {0: "源"}, [], {0: (2.0, 3.2)}, LLM, RepairConfig())
    assert n == 1
    assert out[0].text == "Câu ngắn"
    assert [r.sub_id for r in out] == list(range(len(out)))

    # rewrite is not shorter -> rejected, original kept
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\tMột câu còn dài hơn cả bản gốc lê thê dài dòng thêm nữa")
    out, n = repair(rows, {0: "源"}, [], {0: (2.0, 3.2)}, LLM, RepairConfig())
    assert n == 0
    assert out[0].text.startswith("Một câu rất dài")


def test_repair_rejects_han(monkeypatch):
    rows = [SpeechRow(0, 5, 0.0, 2.0, "10-10", "Một câu dài cần rút gọn")]
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\t还有中文")
    out, n = repair(rows, {0: "源"}, [], {0: (2.0, 3.0)}, LLM, RepairConfig())
    assert n == 0


def test_render_config_defaults():
    cfg = RenderConfig(provider_url="http://x")
    assert cfg.speaker == "BV421_vivn_streaming"
    assert cfg.tempo_ceiling == 1.18
    assert cfg.overflow_tolerance == 0.4


def test_expand_completes_underfilled_keeps_complete(monkeypatch):
    from backend.app.dubbing.repair import expand

    rows = [SpeechRow(0, 5, 0.0, 6.0, "10-10", "Lời ngắn")]

    # LLM restores dropped content (much longer) -> accepted
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\tLời đầy đủ hơn nhiều với toàn bộ ý của câu gốc được khôi phục lại")
    out, n = expand(rows, {0: "源"}, [], {0: (6.0, 2.0)}, LLM, RepairConfig())
    assert n == 1
    assert "đầy đủ" in out[0].text

    # LLM judges it complete and returns roughly the same -> kept unchanged
    monkeypatch.setattr(repair_mod, "complete", lambda s, u, c: "0\tLời ngắn")
    out, n = expand(rows, {0: "源"}, [], {0: (6.0, 2.0)}, LLM, RepairConfig())
    assert n == 0
    assert out[0].text == "Lời ngắn"

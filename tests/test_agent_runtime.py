import json

import pytest

from backend.app.agents import AgentConfig, AgentError, runtime
from backend.app.agents.agy import build_agy_argv, format_timeout, parse_agy_output
from backend.app.agents.command import build_command_argv


def test_format_timeout_renders_go_durations():
    assert format_timeout(300) == "5m0s"
    assert format_timeout(90) == "1m30s"
    assert format_timeout(0.4) == "0m1s"


def test_build_agy_argv_carries_model_effort_and_timeout():
    config = AgentConfig(bin="agy", model="gemini-3.8-flash-low", effort="low", timeout=120)
    argv = build_agy_argv("dịch đoạn này", config)
    assert argv[0] == "agy"
    assert argv[1] == "-p" and argv[2] == "dịch đoạn này"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--print-timeout") + 1] == "2m0s"
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-low"
    assert argv[argv.index("--effort") + 1] == "low"


def test_build_agy_argv_omits_effort_when_unset():
    argv = build_agy_argv("hi", AgentConfig(bin="agy", model="gemini-3.8-flash-low"))
    assert "--effort" not in argv
    assert "--model" in argv


def test_parse_agy_output_reads_last_envelope_and_reports_errors():
    ok = json.dumps({"status": "SUCCESS", "response": "xin chào"})
    assert parse_agy_output(f"scanner chatter\n{ok}") == "xin chào"

    err = json.dumps({"status": "ERROR", "error": "You have exhausted your quota on this model."})
    with pytest.raises(AgentError, match="quota"):
        parse_agy_output(err)


def test_parse_agy_output_requires_envelope():
    with pytest.raises(AgentError, match="no result envelope"):
        parse_agy_output("plain text only")


def test_build_command_argv_substitutes_or_appends_prompt():
    argv = build_command_argv("acpx claude exec --format quiet {prompt}", "hello")
    assert argv == ["acpx", "claude", "exec", "--format", "quiet", "hello"]
    assert build_command_argv("codex exec --json", "hi") == ["codex", "exec", "--json", "hi"]


def test_run_once_dispatches_providers(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "run_agy", lambda prompt, config: calls.append(("agy", prompt)) or "A")
    monkeypatch.setattr(runtime, "run_command", lambda prompt, config: calls.append(("cmd", prompt)) or "C")

    assert runtime.run_once("p1", AgentConfig(provider="agy")) == "A"
    assert runtime.run_once("p2", AgentConfig(provider="command", command="tool {prompt}")) == "C"
    assert calls == [("agy", "p1"), ("cmd", "p2")]

    with pytest.raises(AgentError, match="requires agent_command"):
        runtime.run_once("p3", AgentConfig(provider="command"))


def test_complete_retries_then_succeeds(monkeypatch):
    state = {"n": 0}

    def fake_run_once(prompt, config):
        state["n"] += 1
        if state["n"] < 3:
            raise AgentError("transient")
        return f"ok:{prompt[:3]}"

    monkeypatch.setattr(runtime, "run_once", fake_run_once)
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    assert runtime.complete("SYS", "USER", AgentConfig(retries=3)) == "ok:SYS"
    assert state["n"] == 3


def test_complete_raises_after_last_attempt(monkeypatch):
    monkeypatch.setattr(runtime, "run_once", lambda prompt, config: (_ for _ in ()).throw(AgentError("nope")))
    monkeypatch.setattr(runtime.time, "sleep", lambda _seconds: None)
    with pytest.raises(AgentError, match="after 2 attempts"):
        runtime.complete("s", "u", AgentConfig(retries=2))


def test_compose_prompt_keeps_system_then_user():
    text = runtime.compose_prompt("SYS", "USER")
    assert text.startswith("SYS") and text.endswith("USER")
    assert runtime.compose_prompt("", "USER") == "USER"

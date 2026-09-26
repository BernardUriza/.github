"""The subscription path goes through the unmodified `claude` binary, isolated.

Por qué existe (2026-09-26): bair mandaba el CLAUDE_CODE_OAUTH_TOKEN directo a
/v1/messages con una identidad de Claude Code simulada. Los términos de Anthropic
reservan el OAuth de suscripción para el uso ordinario de Claude Code y las apps
nativas (code.claude.com/docs/en/legal-and-compliance); el camino permitido es el
binario. Y como el binario carga hooks/settings/MCP de su cwd y de HOME, y un PR
controla el checkout, estos tests fijan el aislamiento, no sólo el parseo.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bair.pipelines import gatekeep

VERDICT = {"verdict": "APPROVE", "severity": "LOW", "summary": "ok", "issues": [], "recommendation": ""}


class _Proc:
    def __init__(self, payload, returncode=0, stderr=""):
        self.stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def cli(monkeypatch):
    """A fake `claude` on PATH that records how it was invoked."""
    seen: dict = {}
    replies: list = []

    def fake_run(cmd, *, input, cwd, env, capture_output, text, timeout):
        seen.update(cmd=cmd, input=input, cwd=cwd, env=dict(env), timeout=timeout)
        idx = cmd.index("--system-prompt-file") + 1
        seen["system"] = Path(cmd[idx]).read_text(encoding="utf-8")
        seen["cwd_listing"] = sorted(os.listdir(cwd))
        return replies.pop(0)

    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/claude")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "ghs_should_never_reach_the_binary")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-reach-the-binary")
    return seen, replies


def test_a_successful_run_is_parsed_and_normalized(cli):
    seen, replies = cli
    replies.append(_Proc({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(VERDICT)}))
    d = gatekeep._call_claude_code("SYSTEM PROMPT", "THE DIFF", "tok", model="claude-opus-4-7")
    assert (d.verdict, d.provider) == ("APPROVE", "claude-code")
    assert seen["input"] == "THE DIFF"
    assert seen["system"] == "SYSTEM PROMPT"
    cmd = seen["cmd"]
    assert cmd[:2] == ["/fake/bin/claude", "-p"]
    assert cmd[cmd.index("--disallowedTools") + 1] == "*"
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-7"
    assert "--no-session-persistence" in cmd and cmd[cmd.index("--max-turns") + 1] == "1"


def test_the_binary_runs_isolated_from_the_pr_and_the_jobs_secrets(cli):
    seen, replies = cli
    replies.append(_Proc({"subtype": "success", "is_error": False, "result": json.dumps(VERDICT)}))
    gatekeep._call_claude_code("s", "u", "tok")
    env = seen["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert "GH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["HOME"] == seen["cwd"] and env["CLAUDE_CONFIG_DIR"].startswith(seen["cwd"])
    assert seen["cwd"] != os.getcwd()  # never the PR checkout: its .claude/ hooks would run
    assert seen["cwd_listing"] == ["gatekeep_system.txt"]  # an empty room with only our prompt
    assert not os.path.exists(seen["cwd"])  # and it is gone afterwards


def test_a_cli_error_raises_with_the_reason(cli):
    _seen, replies = cli
    replies.append(_Proc({"subtype": "error_during_execution", "is_error": True, "api_error_status": 429, "result": "rate limited"}))
    with pytest.raises(RuntimeError, match=r"error_during_execution \(api status 429\): rate limited"):
        gatekeep._call_claude_code("s", "u", "tok")


def test_unreadable_output_raises_with_stderr(cli):
    _seen, replies = cli
    replies.append(_Proc("not json", returncode=1, stderr="Invalid API key"))
    with pytest.raises(RuntimeError, match="exit 1, unreadable output: Invalid API key"):
        gatekeep._call_claude_code("s", "u", "tok")


def test_a_missing_binary_abstains_through_the_fallback_chain(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    d = gatekeep._call_llm("s", "u")
    assert d.verdict == "UNAVAILABLE"
    assert "claude-code: claude CLI not found" in d.summary


def test_the_subscription_token_is_never_sent_to_the_messages_api_directly():
    source = Path(gatekeep.__file__).read_text(encoding="utf-8")
    assert "oauth-2025-04-20" not in source
    assert "You are Claude Code, Anthropic's official CLI" not in source

"""The gate reviews through AIRE, Bernard's canonical source of LLM agents.

Por qué existe (2026-09-26): BAIR hablaba con el modelo por su cuenta (token OAuth
directo, luego el binario `claude`, luego una API key que nadie tenía). AIRE ya es
la fuente canónica de agentes del ecosistema — server-bot habla con él por
``fi_runner.AIREBackend`` — así que el gate entra como un consumidor más, con su
propio token revocable, y AIRE pone el modelo, el transcript y el gasto.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import fi_runner
import pytest

from bair.pipelines import gatekeep

VERDICT = {"verdict": "APPROVE", "severity": "LOW", "summary": "ok", "issues": [], "recommendation": ""}


@dataclass
class _Result:
    text: str
    model: str = "claude-opus-4-7"
    usage: dict = field(default_factory=dict)


@pytest.fixture
def aire(monkeypatch):
    seen: dict = {"backends": []}

    class FakeBackend:
        def __init__(self, project, *, gate_url=None, auth_token=None, default_mode="complete", timeout=300.0, **kw):
            seen["backends"].append(
                {"project": project, "gate_url": gate_url, "token": auth_token, "mode": default_mode}
            )

        async def run_turn(self, *, system_prompt, user_message, mcp_servers, tool_policy, model=None, session_id=None):
            seen["turn"] = {
                "system": system_prompt,
                "user": user_message,
                "mcp": mcp_servers,
                "policy": tool_policy,
                "model": model,
                "session": session_id,
            }
            return seen.get("reply") or _Result(json.dumps(VERDICT))

    monkeypatch.setattr(fi_runner, "AIREBackend", FakeBackend)
    monkeypatch.delenv("AIRE_GATE_URL", raising=False)
    return seen


def test_a_review_is_one_complete_turn_in_a_prompt_named_casita(aire):
    d = gatekeep._call_aire("SYSTEM", "THE DIFF", "tok-bair", model="claude-opus-4-7")
    assert (d.verdict, d.provider) == ("APPROVE", "aire")
    backend = aire["backends"][0]
    assert backend["token"] == "tok-bair" and backend["mode"] == "complete"
    assert backend["gate_url"] == "https://gate.bernarduriza.com"
    assert backend["project"] == gatekeep._aire_casita("SYSTEM")
    turn = aire["turn"]
    assert (turn["system"], turn["user"], turn["mcp"], turn["session"]) == ("SYSTEM", "THE DIFF", [], None)
    assert turn["model"] == "claude-opus-4-7"


def test_different_prompts_never_share_a_casita():
    a, b = gatekeep._aire_casita("prompt v1"), gatekeep._aire_casita("prompt v2")
    assert a != b and a == gatekeep._aire_casita("prompt v1")
    assert a.startswith("bair-gatekeep-") and len(a) <= 128
    assert all(c.isalnum() or c in "-_" for c in a)  # AIRE's name allowlist


def test_aire_is_tried_first_and_a_failure_falls_through(aire, monkeypatch):
    monkeypatch.setenv("AIRE_BAIR_TOKEN", "tok-bair")
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert gatekeep._call_llm("s", "u").provider == "aire"

    aire["reply"] = _Result("the door is sleeping")
    d = gatekeep._call_llm("s", "u")
    assert d.verdict == "UNAVAILABLE" and "aire:" in d.summary


def test_the_gate_url_is_configurable(aire, monkeypatch):
    monkeypatch.setenv("AIRE_GATE_URL", "https://staging.example")
    gatekeep._call_aire("s", "u", "tok")
    assert aire["backends"][0]["gate_url"] == "https://staging.example"


def test_the_gate_never_touches_a_subscription_token():
    """Anthropic reserves subscription OAuth for Claude Code and native apps; AIRE
    owns that question for the ecosystem. bair must not read or forward one."""
    from pathlib import Path

    source = Path(gatekeep.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"' not in source
    assert "oauth-2025-04-20" not in source

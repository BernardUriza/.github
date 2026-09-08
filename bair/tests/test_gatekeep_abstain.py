"""Sin cuota, BAIR no tiene poder: se abstiene en voz alta y no bloquea.

Por qué existe (2026-09-08, free-intelligence PR #464): el Max OAuth agotó su
ventana de 5 h (429 en dos corridas) y el ANTHROPIC_API_KEY del repo estaba
muerto (401). BAIR "falló cerrado" y bloqueó una PR con todos los tests verdes
por una falla de infraestructura, no del diff. Un revisor sin modelo no tiene
opinión; lo peligroso del viejo fail-open era el SILENCIO, no el exit 0.
"""

from __future__ import annotations

from bair.pipelines import gatekeep


def test_no_provider_abstains_with_the_reasons(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def boom_oauth(system, user, token):
        raise RuntimeError('Claude OAuth HTTP 429: {"type":"error","error":{"type":"rate_limit_error"}}')

    def boom_anthropic(system, user, key):
        raise RuntimeError("Anthropic HTTP 401: API key is invalid.")

    monkeypatch.setattr(gatekeep, "_call_claude_oauth", boom_oauth)
    monkeypatch.setattr(gatekeep, "_call_anthropic", boom_anthropic)
    d = gatekeep._call_llm("sys", "user")
    assert d.verdict == "UNAVAILABLE"
    assert d.provider == "none"
    assert "does NOT block" in d.summary
    assert "claude-oauth: Claude OAuth HTTP 429" in d.summary
    assert "anthropic: Anthropic HTTP 401" in d.summary


def test_no_credential_at_all_abstains(monkeypatch):
    for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    d = gatekeep._call_llm("sys", "user")
    assert d.verdict == "UNAVAILABLE"
    assert "no LLM credential configured" in d.summary


def test_only_block_gates_the_merge():
    assert gatekeep._exit_code("BLOCK") == 1
    assert gatekeep._exit_code("UNAVAILABLE") == 0
    assert gatekeep._exit_code("WARN") == 0
    assert gatekeep._exit_code("APPROVE") == 0


def test_abstention_comment_is_loud_and_says_it_is_not_blocking():
    body = gatekeep._render_comment(gatekeep._abstain(["claude-oauth: Claude OAuth HTTP 429"]))
    assert "ABSTAINED" in body
    assert "not blocking" in body
    assert "claude-oauth: Claude OAuth HTTP 429" in body
    assert "fails CLOSED" not in body

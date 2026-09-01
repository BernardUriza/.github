"""El OAuth del gatekeeper reintenta la contención y no reintenta lo muerto.

Por qué existe (2026-08-31, discord-bot PRs #56/#57): el Max OAuth es un pool
compartido con las sesiones vivas del dueño. Cinco corridas del gatekeeper en
una tarde murieron al PRIMER 429 — un batch de CI que podía esperar minutos
gratis se declaró UNAVAILABLE cinco veces, y la salida equivocada que casi se
toma fue agregar una API key metered (contra la doctrina del repo consumidor:
"OAuth is canonical").

Regla del mutador: positivo (429 transitorio se recupera) + resistencia
(401 muerto NO se reintenta — quemar 4 intentos contra una credencial
inválida es dead air sin diagnóstico).
"""

from __future__ import annotations

import httpx
import pytest

from bair.pipelines import gatekeep


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = str(self._payload)[:300]

    def json(self):
        return self._payload


_OK_PAYLOAD = {
    "content": [
        {
            "text": '{"verdict": "APPROVE", "severity": "LOW", "summary": "ok", "issues": [], "recommendation": ""}'
        }
    ]
}


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    return slept


def test_transient_429_recovers(monkeypatch, no_sleep):
    calls: list[int] = []
    responses = [_FakeResponse(429), _FakeResponse(429), _FakeResponse(200, _OK_PAYLOAD)]

    def fake_post(url, **kwargs):
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr(httpx, "post", fake_post)
    decision = gatekeep._call_claude_oauth("sys", "user", "tok")
    assert decision.verdict == "APPROVE"
    assert decision.provider == "claude-oauth"
    assert len(calls) == 3
    assert len(no_sleep) == 2


def test_retry_after_header_is_honored_when_sane(monkeypatch, no_sleep):
    responses = [_FakeResponse(429, headers={"retry-after": "17"}), _FakeResponse(200, _OK_PAYLOAD)]
    calls: list[int] = []

    def fake_post(url, **kwargs):
        calls.append(1)
        return responses[len(calls) - 1]

    monkeypatch.setattr(httpx, "post", fake_post)
    gatekeep._call_claude_oauth("sys", "user", "tok")
    assert no_sleep == [17]


def test_dead_credential_401_does_not_retry(monkeypatch, no_sleep):
    calls: list[int] = []

    def fake_post(url, **kwargs):
        calls.append(1)
        return _FakeResponse(401)

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(RuntimeError, match="401"):
        gatekeep._call_claude_oauth("sys", "user", "tok")
    assert len(calls) == 1
    assert no_sleep == []


def test_sustained_429_exhausts_and_fails_closed(monkeypatch, no_sleep):
    calls: list[int] = []

    def fake_post(url, **kwargs):
        calls.append(1)
        return _FakeResponse(429)

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(RuntimeError, match="429"):
        gatekeep._call_claude_oauth("sys", "user", "tok")
    assert len(calls) == gatekeep._OAUTH_MAX_ATTEMPTS
    assert len(no_sleep) == gatekeep._OAUTH_MAX_ATTEMPTS - 1

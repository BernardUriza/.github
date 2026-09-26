"""The repo-tools wiring: off by default, the GitHub token stays on the runner,
and a failed /prepare degrades to the plain review instead of crashing."""

from __future__ import annotations

from types import SimpleNamespace

import httpx

from bair.pipelines import gatekeep
from bair.prompts import load_prompt

BASE, HEAD = "a" * 40, "b" * 40


def _env(monkeypatch, on: bool = True) -> None:
    monkeypatch.setenv("BAIR_REPO_TOOLS", "on" if on else "")
    monkeypatch.setenv("BAIR_REPO_MCP_URL", "https://repo-mcp.example.com/")
    monkeypatch.setenv("BAIR_REPO_MCP_TOKEN", "mcp-bearer")
    monkeypatch.setenv("GH_TOKEN", "ghs_runner_only")


def _capture_llm(monkeypatch) -> dict:
    seen: dict = {}

    def fake_llm(system, user, model=None, repo_tools=None):
        seen.update(system=system, repo_tools=repo_tools)
        return gatekeep.GatekeepDecision("APPROVE", "LOW", "ok", [], "", "aire")

    monkeypatch.setattr(gatekeep, "_call_llm", fake_llm)
    monkeypatch.setattr(gatekeep, "gather_playbook_rules", lambda: "")
    return seen


def test_off_by_default_never_calls_prepare(monkeypatch, tmp_path):
    _env(monkeypatch, on=False)
    seen = _capture_llm(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("prepare called")))
    gatekeep.review("diff --git a/x b/x\n+1\n", str(tmp_path), "o/r", "1", base_sha=BASE, head_sha=HEAD)
    assert seen["repo_tools"] is None
    assert seen["system"] == load_prompt("gatekeep_system")


def test_on_pins_a_capsule_and_keeps_the_github_token_on_the_runner(monkeypatch, tmp_path):
    _env(monkeypatch)
    seen = _capture_llm(monkeypatch)
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, headers, json))
        return SimpleNamespace(status_code=200, json=lambda: {"capsule": "cap123"}, text="")

    monkeypatch.setattr(httpx, "post", fake_post)
    gatekeep.review("diff --git a/x b/x\n+1\n", str(tmp_path), "o/r", "1", base_sha=BASE, head_sha=HEAD)

    (url, headers, body), = calls
    assert url == "https://repo-mcp.example.com/prepare"
    assert headers["X-GitHub-Token"] == "ghs_runner_only"
    assert body == {"repo": "o/r", "base_sha": BASE, "head_sha": HEAD}

    spec = seen["repo_tools"]
    assert spec.url == "https://repo-mcp.example.com/mcp/cap123"
    assert spec.headers == {"Authorization": "Bearer mcp-bearer"}
    assert "ghs_runner_only" not in repr(spec) + str(spec.headers)
    assert seen["system"].endswith(load_prompt("gatekeep_repo_tools"))


def test_failed_prepare_degrades_to_the_plain_review(monkeypatch, tmp_path):
    _env(monkeypatch)
    seen = _capture_llm(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: SimpleNamespace(status_code=422, text="nope", json=dict))
    d = gatekeep.review("diff --git a/x b/x\n+1\n", str(tmp_path), "o/r", "1", base_sha=BASE, head_sha=HEAD)
    assert d.verdict == "APPROVE"
    assert seen["repo_tools"] is None
    assert seen["system"] == load_prompt("gatekeep_system")

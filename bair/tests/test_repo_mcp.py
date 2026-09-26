"""The repo MCP against a real local origin: capsule pinning, read-only tools, and
the doors that must stay shut (no bearer, unlisted repo, model-chosen flags)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bair.repo_mcp import app as app_mod
from bair.repo_mcp import tools

TOKEN = "t" * 32
AUTH = {"authorization": f"Bearer {TOKEN}"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
                               "GIT_COMMITTER_EMAIL": "a@x", "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
                               "HOME": str(cwd)}).stdout.strip()


@pytest.fixture
def origin(tmp_path, monkeypatch):
    repo = tmp_path / "origin" / "acme" / "widget.git"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def pay(amount):\n    return amount\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("def pay(amount):\n    return amount * 2\n\nSECRET_PATTERN = 1\n")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "head")
    head = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("REPO_MCP_GIT_BASE", str(tmp_path / "origin"))
    monkeypatch.setenv("REPO_MCP_DATA", str(tmp_path / "mirrors"))
    monkeypatch.setenv("REPO_MCP_REPOS", "acme/widget")
    monkeypatch.setenv("REPO_MCP_TOKEN", TOKEN)
    app_mod._capsules.clear()
    return base, head


@pytest.fixture
def client():
    return TestClient(app_mod.app)


def _prepare(client, base, head, repo="acme/widget"):
    return client.post("/prepare", headers=AUTH, json={"repo": repo, "base_sha": base, "head_sha": head})


def _call(client, capsule, name, **arguments):
    r = client.post(f"/mcp/{capsule}", headers=AUTH, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200
    res = r.json()["result"]
    return res["content"][0]["text"], res["isError"]


def test_without_bearer_every_door_is_404(client, origin):
    base, head = origin
    assert client.post("/prepare", json={"repo": "acme/widget", "base_sha": base, "head_sha": head}).status_code == 404
    assert client.post("/prepare", headers={"authorization": "Bearer nope"}, json={}).status_code == 404
    assert client.get("/health").status_code == 200


def test_unlisted_repo_and_short_sha_are_refused(client, origin):
    base, head = origin
    assert _prepare(client, base, head, repo="acme/other").status_code == 422
    assert _prepare(client, base[:12], head).status_code == 422


def test_capsule_reads_head_and_base(client, origin):
    base, head = origin
    r = _prepare(client, base, head)
    assert r.status_code == 200, r.text
    cap = r.json()["capsule"]

    text, err = _call(client, cap, "read_file", path="src/app.py")
    assert not err and "amount * 2" in text
    text, err = _call(client, cap, "read_file", path="src/app.py", rev="base")
    assert not err and "amount * 2" not in text and "return amount" in text

    text, err = _call(client, cap, "grep", pattern="SECRET_PATTERN")
    assert not err and "src/app.py:4:" in text
    text, err = _call(client, cap, "list_files", path="src")
    assert not err and "src/app.py" in text
    text, err = _call(client, cap, "diff")
    assert not err and "+    return amount * 2" in text
    text, err = _call(client, cap, "git_log")
    assert not err and "head" in text and "base" in text
    text, err = _call(client, cap, "blame", path="src/app.py", start_line=1, end_line=2)
    assert not err and "amount * 2" in text


def test_second_prepare_reuses_the_permanent_mirror(client, origin):
    base, head = origin
    assert _prepare(client, base, head).status_code == 200
    mirror_dir = Path(app_mod.mirror.data_dir()) / "acme__widget.git"
    assert mirror_dir.is_dir()
    assert _prepare(client, base, head).status_code == 200


def test_model_arguments_cannot_become_git_flags(client, origin):
    base, head = origin
    cap = _prepare(client, base, head).json()["capsule"]
    for path in ("--output=/tmp/x", ":(top)x", "-c"):
        text, err = _call(client, cap, "read_file", path=path)
        assert err, path
    text, err = _call(client, cap, "grep", pattern="--open-files-in-pager=sh")
    assert not err and text == "(no matches)"
    text, err = _call(client, cap, "grep", pattern="x", path_glob="--exec=sh")
    assert err
    text, err = _call(client, cap, "read_file", path="src/app.py", rev=head)
    assert err


def test_unknown_or_expired_capsule_is_404(client, origin):
    assert client.post("/mcp/nope", headers=AUTH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 404


def test_tools_list_is_read_only_surface(client, origin):
    base, head = origin
    cap = _prepare(client, base, head).json()["capsule"]
    r = client.post(f"/mcp/{cap}", headers=AUTH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert names == set(tools.TOOLS) == {"list_files", "read_file", "grep", "git_log", "blame", "diff"}

"""The permanent, read-only mirror a review reads from.

One bare clone per repository under ``$REPO_MCP_DATA``. ``prepare`` is the only
writer: it proves the caller can read the repository (an authenticated
``ls-remote`` on every call, even when the commits are already cached) and
fetches the two commits of the review. Everything else is ``git`` plumbing that
reads objects at a pinned commit — no worktree, no checkout, nothing executed.

The GitHub token a caller presents is used for that one fetch and never touches
the disk: it rides ``GIT_CONFIG_*`` in the child's environment, not argv and not
the mirror's config.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
GIT_TIMEOUT_S = 25
FETCH_TIMEOUT_S = 240


class MirrorError(Exception):
    """A refusal or failure whose message is safe to hand back to the caller."""


@dataclass(frozen=True)
class GitResult:
    out: str
    truncated: bool


def data_dir() -> Path:
    return Path(os.environ.get("REPO_MCP_DATA", "/data/mirrors"))


def _clone_url(repo: str) -> str:
    base = os.environ.get("REPO_MCP_GIT_BASE", "https://github.com")
    return f"{base.rstrip('/')}/{repo}.git"


def mirror_path(repo: str) -> Path:
    return data_dir() / (repo.replace("/", "__") + ".git")


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(repo: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(repo, threading.Lock())


def _auth_env(token: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {basic}"
    return env


def _git(args: list[str], *, git_dir: Path | None = None, env: dict[str, str] | None = None,
         timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    cmd = ["git"] + (["--git-dir", str(git_dir)] if git_dir else []) + args
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env=env or _auth_env(""), check=False)
    except subprocess.TimeoutExpired as exc:
        raise MirrorError(f"git {args[0]} timed out after {timeout}s") from exc


def prepare(repo: str, base_sha: str, head_sha: str, token: str) -> None:
    """Make both commits readable in the mirror, after proving read access."""
    if not REPO.match(repo) or ".." in repo:
        raise MirrorError("repo must look like owner/name")
    allowed = {r.strip().lower() for r in os.environ.get("REPO_MCP_REPOS", "").split(",") if r.strip()}
    if repo.lower() not in allowed:
        raise MirrorError(f"{repo} is not in REPO_MCP_REPOS")
    for sha in (base_sha, head_sha):
        if not SHA.match(sha):
            raise MirrorError("base_sha and head_sha must be full 40-char hex SHAs")
    env = _auth_env(token)
    url = _clone_url(repo)
    probe = _git(["ls-remote", "--heads", url], env=env, timeout=60)
    if probe.returncode != 0:
        raise MirrorError(f"cannot read {repo} with the presented credential")
    path = mirror_path(repo)
    with _lock_for(repo):
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            init = _git(["init", "--bare", "--quiet", str(path)])
            if init.returncode != 0:
                raise MirrorError("could not create the mirror")
        missing = [s for s in (base_sha, head_sha) if not has_commit(repo, s)]
        if missing:
            fetch = _git(["fetch", "--quiet", "--no-tags", url, *missing],
                         git_dir=path, env=env, timeout=FETCH_TIMEOUT_S)
            if fetch.returncode != 0:
                raise MirrorError(f"fetch of {len(missing)} commit(s) from {repo} failed")
        for sha in (base_sha, head_sha):
            if not has_commit(repo, sha):
                raise MirrorError(f"commit {sha[:12]} is not in {repo}")


def has_commit(repo: str, sha: str) -> bool:
    path = mirror_path(repo)
    if not path.exists():
        return False
    return _git(["cat-file", "-e", f"{sha}^{{commit}}"], git_dir=path).returncode == 0


def read(repo: str, args: list[str], *, max_chars: int) -> GitResult:
    """Run one read-only plumbing command against the mirror, output capped."""
    proc = _git(args, git_dir=mirror_path(repo))
    if proc.returncode not in (0, 1):
        raise MirrorError(proc.stderr.strip().splitlines()[-1][:300] if proc.stderr.strip() else "git failed")
    out = proc.stdout
    if len(out) > max_chars:
        return GitResult(out[:max_chars], True)
    return GitResult(out, False)

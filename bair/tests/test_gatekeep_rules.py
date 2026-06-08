"""Tests for repo-specific rule ingestion in the gatekeep pipeline.

These cover the new ``gather_repo_rules`` capability: it must read the
target repo's own ``.claude/rules/*.md`` (and ``.github/instructions/
*.md``) at the PR head SHA and fold them into the review system prompt,
while staying a strict no-op when the repo defines no such rules.
"""

from __future__ import annotations

import pytest

from bair.pipelines import gatekeep


class RecordingGitHub:
    """Minimal GitHubClient fake driven by a routing function.

    ``responder(args)`` receives the tuple passed to ``run_gh`` and
    returns the canned stdout (or raises to simulate an API error).
    """

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[tuple[str, ...]] = []

    def run_gh(self, *args: str, check: bool = True, input_data=None) -> str:
        self.calls.append(args)
        return self._responder(args)

    def run_git(self, *args: str, check: bool = True, cwd=None) -> str:  # pragma: no cover
        return ""


def _joined(args: tuple[str, ...]) -> str:
    return " ".join(args)


def test_no_rules_dirs_returns_empty_string():
    """A repo with no rule dirs (404) yields no prompt fragment — the
    system prompt stays byte-identical to legacy behavior."""

    def responder(args):
        return '{"message":"Not Found","status":"404"}'

    gh = RecordingGitHub(responder)
    out = gatekeep.gather_repo_rules(gh, "owner/repo", "deadbeef")
    assert out == ""


def test_ingests_claude_rules_and_marks_section():
    """Rule files are listed, fetched at the ref, and wrapped in the
    repository-specific section with their path as a header."""
    rule_body = (
        "# Framework-First\n\nPotential framework abstraction leak: reusable "
        "chat/agent conversation logic added in consumer app. Move to "
        "fi-glass/core or document why this is app-specific."
    )

    def responder(args):
        joined = _joined(args)
        if "contents/.claude/rules?ref=" in joined:
            return ".claude/rules/framework-first-canary.md\n"
        if "contents/.github/instructions?ref=" in joined:
            return '{"message":"Not Found","status":"404"}'
        if "framework-first-canary.md?ref=" in joined:
            return rule_body
        return ""

    gh = RecordingGitHub(responder)
    out = gatekeep.gather_repo_rules(gh, "owner/repo", "abc1234def")

    assert "REPOSITORY-SPECIFIC GATEKEEPING RULES" in out
    assert "### .claude/rules/framework-first-canary.md" in out
    assert "Potential framework abstraction leak" in out
    assert any("ref=abc1234def" in _joined(c) for c in gh.calls)


def test_truncation_first_file_overflows(monkeypatch):
    """When the very first file already exceeds the cap, nothing is
    emitted (and no crash)."""
    monkeypatch.setattr(gatekeep, "_MAX_RULE_CHARS", 200)
    big = "x" * 500

    def responder(args):
        joined = _joined(args)
        if "contents/.claude/rules?ref=" in joined:
            return "a.md\nb.md\n"
        if "contents/.github/instructions?ref=" in joined:
            return ""
        if "a.md?ref=" in joined or "b.md?ref=" in joined:
            return big
        return ""

    gh = RecordingGitHub(responder)
    out = gatekeep.gather_repo_rules(gh, "owner/repo", "sha")
    assert out == ""


def test_partial_truncation_keeps_first_file(monkeypatch):
    """When the first file fits but the second would overflow, the first
    is kept and a truncation note is appended."""
    monkeypatch.setattr(gatekeep, "_MAX_RULE_CHARS", 120)

    def responder(args):
        joined = _joined(args)
        if "contents/.claude/rules?ref=" in joined:
            return "a.md\nb.md\n"
        if "contents/.github/instructions?ref=" in joined:
            return ""
        if "a.md?ref=" in joined:
            return "short rule A"
        if "b.md?ref=" in joined:
            return "y" * 500
        return ""

    gh = RecordingGitHub(responder)
    out = gatekeep.gather_repo_rules(gh, "owner/repo", "sha")

    assert "### a.md" in out
    assert "short rule A" in out
    assert "### b.md" not in out
    assert "truncated" in out.lower()


def test_non_md_files_are_ignored():
    """Only ``*.md`` files are ingested; other files in the dir are skipped."""

    def responder(args):
        joined = _joined(args)
        if "contents/.claude/rules?ref=" in joined:
            return ".claude/rules/keep.md\n.claude/rules/README.txt\n"
        if "contents/.github/instructions?ref=" in joined:
            return ""
        if "keep.md?ref=" in joined:
            return "real rule content"
        return ""

    gh = RecordingGitHub(responder)
    out = gatekeep.gather_repo_rules(gh, "owner/repo", "sha")

    assert "### .claude/rules/keep.md" in out
    assert "README.txt" not in out
    assert not any("README.txt?ref=" in _joined(c) for c in gh.calls)


def test_api_error_is_swallowed():
    """A raising GitHub client during listing yields no rules, not a crash."""

    def responder(args):
        raise RuntimeError("network down")

    gh = RecordingGitHub(responder)
    out = gatekeep.gather_repo_rules(gh, "owner/repo", "sha")
    assert out == ""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))

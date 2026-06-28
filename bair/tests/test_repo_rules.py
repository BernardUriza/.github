"""Tests for the repo_rules gatherer + the gatekeep prompt assembly.

The gatherer is pure stdlib (no xair) so these run in isolation. They assert the
gatekeeper actually SEES the target repo's doctrine: rules are read with citable
provenance, the cap holds, a repo with no rules degrades to generic (never errors),
and the assembled payload carries the rules alongside the diff.
"""

from __future__ import annotations

import pytest

from bair.gatherers.repo_rules import gather_repo_rules


def _mk(root, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def test_reads_claude_md_and_rules_with_provenance(tmp_path) -> None:
    _mk(tmp_path, ".claude/CLAUDE.md", "# Project\nUse Make only.")
    _mk(tmp_path, ".claude/rules/framework-first-canary.md", "# Framework first\nElevate to fi-glass.")
    out = gather_repo_rules(tmp_path)
    assert out.startswith("<repository_rules>") and out.endswith("</repository_rules>")
    # provenance is mandatory — the model cites by path
    assert "FILE: .claude/CLAUDE.md" in out
    assert "FILE: .claude/rules/framework-first-canary.md" in out
    assert "Elevate to fi-glass" in out


def test_claude_md_leads_then_priority_then_alpha(tmp_path) -> None:
    _mk(tmp_path, ".claude/CLAUDE.md", "claude")
    _mk(tmp_path, ".claude/rules/zzz-misc.md", "z")
    _mk(tmp_path, ".claude/rules/00-constitution.md", "charter")
    out = gather_repo_rules(tmp_path)
    i_claude = out.index("CLAUDE.md")
    i_const = out.index("00-constitution.md")
    i_zzz = out.index("zzz-misc.md")
    assert i_claude < i_const < i_zzz  # CLAUDE first, charter before misc


def test_no_claude_dir_returns_empty_not_error(tmp_path) -> None:
    assert gather_repo_rules(tmp_path) == ""


def test_cap_truncates_with_marker_and_keeps_priority_first(tmp_path) -> None:
    _mk(tmp_path, ".claude/CLAUDE.md", "head")
    _mk(tmp_path, ".claude/rules/00-constitution.md", "C" * 4000)
    _mk(tmp_path, ".claude/rules/zzz-low-priority.md", "Z" * 4000)
    out = gather_repo_rules(tmp_path, max_bytes=2000)
    assert "[repository rules truncated" in out
    # the charter survives; the low-priority rule is the one dropped
    assert "00-constitution.md" in out
    assert "zzz-low-priority.md" not in out


def test_empty_claude_dir_returns_empty(tmp_path) -> None:
    (tmp_path / ".claude").mkdir()
    assert gather_repo_rules(tmp_path) == ""


# The prompt assembly lives in gatekeep.py, which imports xair at module load.
# Skip cleanly when xair is absent (local dev); CI installs it.
def test_user_msg_carries_rules_and_diff() -> None:
    pytest.importorskip("xair")
    from bair.pipelines.gatekeep import _build_user_msg

    msg = _build_user_msg(
        diff="--- a/x\n+++ b/x\n+leak",
        repo_rules="<repository_rules>\nFILE: .claude/rules/r.md\nno secrets\n</repository_rules>",
        repo="owner/repo",
        pr_num="42",
    )
    assert "Repository rules:" in msg
    assert "FILE: .claude/rules/r.md" in msg
    assert "DIFF:" in msg and "+leak" in msg


def test_user_msg_states_absence_when_no_rules() -> None:
    pytest.importorskip("xair")
    from bair.pipelines.gatekeep import _build_user_msg

    msg = _build_user_msg(diff="d", repo_rules="", repo="o/r", pr_num="1")
    assert "No repository rules found." in msg

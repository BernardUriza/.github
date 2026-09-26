"""Tests for the eval suite: the arithmetic, the packaged cases, and one offline
end-to-end run through worktrees with ``gatekeep.review`` stubbed out."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bair.evals import __main__ as runner
from bair.evals.stats import discordant, majority, summarize, wilson
from bair.pipelines import gatekeep


def test_wilson_matches_known_small_n_bounds():
    lo, hi = wilson(1, 1)
    assert round(lo, 3) == 0.207 and hi == 1.0
    lo, _ = wilson(2, 2)
    assert round(lo, 3) == 0.342
    assert wilson(0, 0) == (0.0, 1.0)


def test_majority_counts_would_block_and_flags_flips():
    assert majority([{"verdict": "WARN", "would_block": True}] * 2 + [{"verdict": "WARN"}]) == ("BLOCK", True)
    assert majority([{"verdict": "APPROVE"}] * 3) == ("APPROVE", False)
    assert majority([{"verdict": "UNAVAILABLE"}]) == ("UNAVAILABLE", False)
    assert majority([{"verdict": "APPROVE"}, {"verdict": "BLOCK"}])[0] == "BLOCK"  # tie → stricter


def test_summarize_and_discordance():
    rows = [
        {"case": "d1", "label": "defect", "context": "full", "verdict": "WARN"},
        {"case": "d1", "label": "defect", "context": "slice", "verdict": "BLOCK"},
        {"case": "c1", "label": "clean", "context": "full", "verdict": "APPROVE"},
        {"case": "c1", "label": "clean", "context": "slice", "verdict": "APPROVE"},
    ]
    s = summarize(rows)
    assert s["full"]["flagged"] == ["d1"] and s["full"]["blocked"] == []
    assert s["slice"]["blocked"] == ["d1"] and s["slice"]["false_block"] == []
    assert discordant(s, "full", "slice") == [("d1", "WARN", "BLOCK")]


def test_packaged_cases_are_balanced_and_complete():
    spec = runner._load_cases(None)
    cases = spec["cases"]
    assert spec["repo"] == "BernardUriza/server-bot"
    for label in ("defect", "clean"):
        for split in ("dev", "heldout"):
            assert sum(c["label"] == label and c["split"] == split for c in cases) == 6
    for c in cases:
        assert len(c["base"]) == 40 and len(c["head"]) == 40
        assert bool(c["fix"]) == (c["label"] == "defect")
        assert (c["label"] == "defect") == bool(c["truth"])
    blob = json.dumps(cases).lower()
    assert "deliberate" not in blob and "planted" not in blob


def test_offline_end_to_end_through_worktrees(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "m.py").write_text("def f(x):\n    return all(x)\n")
    git("add", "-A"); git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (repo / "m.py").write_text("def f(x):\n    return any(x)\n")
    git("commit", "-qam", "intro")
    intro = git("rev-parse", "HEAD")
    (repo / "m.py").write_text("def f(x):\n    return all(x)\n")
    git("commit", "-qam", "fix")
    fix = git("rev-parse", "HEAD")
    cases = {"repo": "o/r", "cases": [
        {"id": "defect-1", "label": "defect", "split": "dev", "base": base, "head": intro, "fix": fix, "truth": "t"},
        {"id": "clean-1", "label": "clean", "split": "heldout", "base": base, "head": intro, "fix": "", "truth": ""},
    ]}
    (tmp_path / "cases.json").write_text(json.dumps(cases))

    seen = []

    def fake_review(diff, root, repo_name, pr, context_mode=None, model=None):
        seen.append((Path(root).name, context_mode, "any(x)" in diff))
        assert model in ("m-old", "m-new")
        blocked = Path(root).name == "defect-1"
        return gatekeep.GatekeepDecision(
            verdict="WARN" if blocked else "APPROVE", severity="CRITICAL" if blocked else "LOW", summary="s",
            issues=[{"type": "correctness", "rule": "data_plane", "severity": "CRITICAL", "message": "m.py:2 flips all→any"}] if blocked else [],
            recommendation="", provider="fake", would_block=blocked,
        )

    monkeypatch.setattr(gatekeep, "review", fake_review)
    out = tmp_path / "rows.jsonl"
    assert runner.main(["--cases", str(tmp_path / "cases.json"), "--repo-dir", str(repo), "--runs", "2",
                        "--context", "full,slice", "--models", "m-old,m-new", "--workers", "2", "--out", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 16 and all(r["prompt_sha"] for r in rows)
    assert {r["config"] for r in rows} == {"full@m-old", "full@m-new", "slice@m-old", "slice@m-new"}
    assert all(diff_has_change for _, _, diff_has_change in seen)
    assert {r["context"] for r in rows} == {"full", "slice"}
    assert all(r["localized"] for r in rows if r["case"] == "defect-1")
    report = capsys.readouterr().out
    assert "defects BLOCKed" in report and "1/1" in report
    assert "Issues carrying a valid `rule`: 8/8" in report
    assert "`clean-1`" not in report  # held-out cases are never listed one by one
    assert not list(tmp_path.glob("bair-eval-*"))
    assert git("worktree", "list").count("\n") == 0  # worktrees cleaned up

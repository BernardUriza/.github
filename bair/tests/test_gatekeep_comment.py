"""Cuándo comenta el gate y cuándo calla.

Por qué existe (2026-11-03): cada corrida hacía un POST y GitHub manda un correo por
comentario; en free-intelligence, 49 de 54 decían APPROVE. Pero callar no puede
revivir la aprobación silenciosa (un 401 durante semanas: check verde, cero revisión):
ABSTAINED y un APPROVE sin proveedor siguen hablando, y el step summary se escribe
siempre.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from bair.pipelines import gatekeep

SHA = "abcdef1234567890"


class FakeGitHub:
    """The issue-comments API, as far as run_gh reaches it."""

    def __init__(self, comments=(), fail=()):
        self.comments = [{"id": i + 1, "author": a, "body": b} for i, (a, b) in enumerate(comments)]
        self.fail = set(fail)  # any of "list", "POST", "PATCH"
        self.calls: list[str] = []

    def run_gh(self, *args, check=True, input_data=None):
        method = args[args.index("--method") + 1] if "--method" in args else "list"
        self.calls.append(method)
        if method in self.fail:
            if not check:  # real run_gh: check=False swallows a failure and returns stdout
                return '{"message": "Resource not accessible"}'
            raise subprocess.CalledProcessError(1, "gh")
        if method == "list":
            assert "--paginate" in args
            return "".join(json.dumps(c) + "\n" for c in self.comments)
        body = args[args.index("-f") + 1].removeprefix("body=")
        if method == "POST":
            self.comments.append({"id": len(self.comments) + 1, "author": "Bot", "body": body})
        else:
            comment_id = int(args[1].rsplit("/", 1)[1])
            next(c for c in self.comments if c["id"] == comment_id)["body"] = body
        return "{}"

    @property
    def writes(self):
        return [c for c in self.calls if c != "list"]


def decision(verdict, provider="aire", summary="the summary"):
    return gatekeep.GatekeepDecision(
        verdict=verdict, severity="LOW", summary=summary, issues=[], recommendation="", provider=provider
    )


def marked(verdict, text="old"):
    return f"<!-- bair-gatekeeper verdict={verdict} -->\n{text}"


def run_gate(monkeypatch, tmp_path, d, gh, summary_env=True):
    """The real command, with only the review and the wire faked."""
    for key, value in (("REPO", "o/r"), ("PR_NUM", "7"), ("BASE_SHA", "base"), ("HEAD_SHA", SHA)):
        monkeypatch.setenv(key, value)
    output, summary = tmp_path / "output", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    if summary_env:
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    else:
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(gatekeep, "_get_diff", lambda base, head: "diff")
    monkeypatch.setattr(gatekeep, "review", lambda *a, **k: d)
    code = 0
    try:
        gatekeep.gatekeep(SimpleNamespace(repo="", pr_num=""), SimpleNamespace(github=gh))
    except SystemExit as exc:
        code = exc.code
    return SimpleNamespace(
        code=code,
        output=output.read_text() if output.exists() else "",
        summary=summary.read_text() if summary.exists() else "",
    )


# -- 1. APPROVE is quiet, and the record survives ------------------------------


def test_approve_does_not_comment_and_writes_the_summary(monkeypatch, tmp_path):
    gh = FakeGitHub()
    r = run_gate(monkeypatch, tmp_path, decision("APPROVE"), gh)
    assert gh.writes == []
    assert gh.comments == []
    assert "APPROVED" in r.summary
    assert "`aire`" in r.summary
    assert "the summary" in r.summary
    assert SHA[:12] in r.summary
    assert "verdict=APPROVE" in r.output
    assert r.code == 0


@pytest.mark.parametrize("verdict", ["APPROVE", "WARN", "BLOCK", "UNAVAILABLE"])
def test_step_summary_is_written_for_every_verdict(monkeypatch, tmp_path, verdict):
    assert gatekeep._VERDICT_HEADERS[verdict] in run_gate(monkeypatch, tmp_path, decision(verdict), FakeGitHub()).summary


def test_no_step_summary_file_logs_the_verdict(monkeypatch, tmp_path):
    logged = []
    monkeypatch.setattr(gatekeep.logger, "warning", logged.append)
    run_gate(monkeypatch, tmp_path, decision("APPROVE"), FakeGitHub(), summary_env=False)
    assert any("no GITHUB_STEP_SUMMARY" in m and "the summary" in m for m in logged)


def test_unwritable_step_summary_logs_and_keeps_the_verdict(monkeypatch, tmp_path):
    for key, value in (("REPO", "o/r"), ("PR_NUM", "7"), ("BASE_SHA", "b"), ("HEAD_SHA", SHA)):
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))  # a directory: open() raises
    logged = []
    monkeypatch.setattr(gatekeep.logger, "warning", logged.append)
    gatekeep._write_step_summary("the record")
    assert any("the record" in m for m in logged)


def test_missing_env_still_leaves_a_summary(monkeypatch, tmp_path):
    for key in ("REPO", "PR_NUM", "BASE_SHA", "HEAD_SHA"):
        monkeypatch.delenv(key, raising=False)
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with pytest.raises(SystemExit):
        gatekeep.gatekeep(SimpleNamespace(repo="", pr_num=""), SimpleNamespace(github=FakeGitHub()))
    assert "did not run" in summary.read_text()


def test_crashed_review_leaves_a_summary_and_still_raises(monkeypatch, tmp_path):
    for key, value in (("REPO", "o/r"), ("PR_NUM", "7"), ("BASE_SHA", "b"), ("HEAD_SHA", SHA)):
        monkeypatch.setenv(key, value)
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(gatekeep, "_get_diff", lambda base, head: "diff")

    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(gatekeep, "review", boom)
    with pytest.raises(RuntimeError):
        gatekeep.gatekeep(SimpleNamespace(repo="", pr_num=""), SimpleNamespace(github=FakeGitHub()))
    assert "crashed" in summary.read_text() and "kaput" in summary.read_text()


# -- 2. What asks for attention still comments ---------------------------------


@pytest.mark.parametrize("verdict", ["WARN", "BLOCK", "UNAVAILABLE"])
def test_first_loud_verdict_posts_with_the_marker(monkeypatch, tmp_path, verdict):
    gh = FakeGitHub()
    run_gate(monkeypatch, tmp_path, decision(verdict), gh)
    assert gh.writes == ["POST"]
    assert gh.comments[0]["body"].startswith(f"<!-- bair-gatekeeper verdict={verdict} -->")
    assert gatekeep._VERDICT_HEADERS[verdict] in gh.comments[0]["body"]


def test_abstention_from_the_real_path_is_loud(monkeypatch, tmp_path):
    gh = FakeGitHub()
    r = run_gate(monkeypatch, tmp_path, gatekeep._abstain(["aire: 401"]), gh)
    assert gh.writes == ["POST"]
    assert "ABSTAINED" in gh.comments[0]["body"] and "aire: 401" in gh.comments[0]["body"]
    assert "abstained=true" in r.output
    assert r.code == 0


def test_approve_with_no_provider_is_not_quiet(monkeypatch, tmp_path):
    """review() answers APPROVE/provider none on an empty diff, and a failed git diff
    returns an empty diff: green check, nobody read the code. It must speak."""
    gh = FakeGitHub()
    run_gate(monkeypatch, tmp_path, decision("APPROVE", provider="none"), gh)
    assert gh.writes == ["POST"]


def test_warn_after_a_silent_approve_posts(monkeypatch, tmp_path):
    """The previous APPROVE left no comment, so there is nothing to edit and no one
    has been told anything yet."""
    gh = FakeGitHub()
    run_gate(monkeypatch, tmp_path, decision("APPROVE"), gh)
    run_gate(monkeypatch, tmp_path, decision("WARN"), gh)
    assert gh.writes == ["POST"]


# -- 3. One comment per PR -----------------------------------------------------


@pytest.mark.parametrize(
    ("prior", "verdict", "write"),
    [
        ("WARN", "WARN", "PATCH"),
        ("BLOCK", "BLOCK", "PATCH"),
        ("UNAVAILABLE", "UNAVAILABLE", "PATCH"),
        ("BLOCK", "WARN", "PATCH"),
        ("WARN", "BLOCK", "POST"),
        ("UNAVAILABLE", "BLOCK", "POST"),
        ("APPROVE", "BLOCK", "POST"),
        ("APPROVE", "WARN", "POST"),
        ("APPROVE", "UNAVAILABLE", "POST"),
        ("WARN", "UNAVAILABLE", "POST"),
        ("BLOCK", "UNAVAILABLE", "POST"),
        ("UNAVAILABLE", "WARN", "POST"),
    ],
)
def test_every_transition(monkeypatch, tmp_path, prior, verdict, write):
    gh = FakeGitHub([("Bot", marked(prior))])
    run_gate(monkeypatch, tmp_path, decision(verdict), gh)
    live = [c for c in gh.comments if f"verdict={verdict} -->" in c["body"]]
    assert gh.calls.count(write) >= 1
    assert len(live) == 1
    if write == "PATCH":
        assert gh.writes == ["PATCH"]
        assert len(gh.comments) == 1
    else:  # the older comment must not stay behind claiming the old verdict
        assert gh.writes == ["POST", "PATCH"]
        assert f"verdict={prior} -->" not in gh.comments[0]["body"]
        assert gh.comments[0]["body"] == gatekeep._SUPERSEDED_BODY


def test_edit_is_on_the_latest_live_comment_and_ignores_superseded(monkeypatch, tmp_path):
    gh = FakeGitHub([("Bot", gatekeep._SUPERSEDED_BODY), ("Bot", marked("WARN"))])
    run_gate(monkeypatch, tmp_path, decision("WARN", summary="fresh"), gh)
    assert gh.comments[0]["body"] == gatekeep._SUPERSEDED_BODY
    assert "fresh" in gh.comments[1]["body"]
    assert gh.writes == ["PATCH"]


@pytest.mark.parametrize(
    "impostor",
    [
        ("User", marked("BLOCK")),  # a person pasting the marker
        ("Bot", f"quoted model text\n{marked('BLOCK')}"),  # marker not at the start
        ("Bot", "## :x: BAIR Gatekeeper: BLOCKED"),  # no marker at all
    ],
)
def test_only_a_bots_leading_marker_counts(monkeypatch, tmp_path, impostor):
    gh = FakeGitHub([impostor])
    before = impostor[1]
    run_gate(monkeypatch, tmp_path, decision("APPROVE"), gh)
    assert gh.writes == []
    assert gh.comments[0]["body"] == before


# -- 4. No stale verdict -------------------------------------------------------


@pytest.mark.parametrize("prior", ["WARN", "BLOCK", "UNAVAILABLE"])
def test_approve_edits_the_older_verdict_away(monkeypatch, tmp_path, prior):
    gh = FakeGitHub([("Bot", marked(prior))])
    run_gate(monkeypatch, tmp_path, decision("APPROVE"), gh)
    assert gh.writes == ["PATCH"]
    assert gh.comments[0]["body"].startswith("<!-- bair-gatekeeper verdict=APPROVE -->")
    assert "APPROVED" in gh.comments[0]["body"]
    assert SHA[:12] in gh.comments[0]["body"]


def test_approve_after_warn_then_block_leaves_no_old_verdict(monkeypatch, tmp_path):
    gh = FakeGitHub()
    for verdict in ("WARN", "BLOCK", "APPROVE"):
        run_gate(monkeypatch, tmp_path, decision(verdict), gh)
    bodies = [c["body"] for c in gh.comments]
    assert len(bodies) == 2
    assert bodies[0] == gatekeep._SUPERSEDED_BODY
    assert bodies[1].startswith("<!-- bair-gatekeeper verdict=APPROVE -->")


# -- 5. A failed comment never changes the verdict -----------------------------


@pytest.mark.parametrize(
    ("verdict", "code"), [("APPROVE", 0), ("WARN", 0), ("BLOCK", 1), ("UNAVAILABLE", 0)]
)
@pytest.mark.parametrize("fail", [{"list"}, {"POST"}, {"PATCH"}, {"list", "POST", "PATCH"}])
def test_comment_failures_never_change_verdict_or_summary(monkeypatch, tmp_path, verdict, code, fail):
    gh = FakeGitHub([("Bot", marked("WARN"))], fail=fail)
    r = run_gate(monkeypatch, tmp_path, decision(verdict), gh)
    assert r.code == code
    assert f"verdict={verdict}" in r.output
    assert gatekeep._VERDICT_HEADERS[verdict] in r.summary


def test_a_failed_post_is_detected_and_reported(monkeypatch, tmp_path):
    """run_gh with check=False swallows a 403; the summary must not claim "posted"."""
    r = run_gate(monkeypatch, tmp_path, decision("UNAVAILABLE"), FakeGitHub(fail={"POST"}))
    assert "FAILED to post" in r.summary


def test_unreadable_thread_still_posts_what_is_loud(monkeypatch, tmp_path):
    gh = FakeGitHub(fail={"list"})
    run_gate(monkeypatch, tmp_path, decision("UNAVAILABLE"), gh)
    assert gh.writes == ["POST"]


def test_unreadable_thread_on_approve_says_so(monkeypatch, tmp_path):
    gh = FakeGitHub(fail={"list"})
    r = run_gate(monkeypatch, tmp_path, decision("APPROVE"), gh)
    assert gh.writes == []
    assert "could not be read" in r.summary


def test_failed_edit_of_a_loud_verdict_falls_back_to_a_new_comment(monkeypatch, tmp_path):
    gh = FakeGitHub([("Bot", marked("WARN"))], fail={"PATCH"})
    r = run_gate(monkeypatch, tmp_path, decision("WARN"), gh)
    assert gh.writes == ["PATCH", "POST", "PATCH"]
    assert "posted" in r.summary


def test_failed_edit_on_approve_reports_the_stale_comment(monkeypatch, tmp_path):
    gh = FakeGitHub([("Bot", marked("WARN"))], fail={"PATCH"})
    r = run_gate(monkeypatch, tmp_path, decision("APPROVE"), gh)
    assert "POST" not in gh.writes
    assert "stale verdict" in r.summary

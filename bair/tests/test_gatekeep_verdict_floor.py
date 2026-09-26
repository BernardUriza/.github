"""El veredicto nunca queda debajo de su issue más severo.

Por qué existe (2026-09-25, backlog 01): en server-bot#104 BAIR reportó un
defecto real del plano de datos como HIGH bajo WARN, el check salió verde y el
PR habría mergeado. El prompt pide que CRITICAL sea BLOCK; esto lo garantiza aunque
el modelo se contradiga.
"""

from __future__ import annotations

from bair.pipelines import gatekeep


def _decision(verdict, severity, *issue_severities):
    return gatekeep.GatekeepDecision(
        verdict=verdict,
        severity=severity,
        summary="s",
        issues=[{"type": "t", "severity": s, "message": "m"} for s in issue_severities],
        recommendation="r",
        provider="p",
    )


def test_a_critical_issue_under_warn_blocks():
    d = gatekeep._floor_verdict(_decision("WARN", "HIGH", "CRITICAL", "MEDIUM"))
    assert d.verdict == "BLOCK"
    assert gatekeep._exit_code(d.verdict) == 1


def test_a_critical_overall_severity_under_approve_blocks():
    assert gatekeep._floor_verdict(_decision("APPROVE", "CRITICAL")).verdict == "BLOCK"


def test_a_high_issue_under_approve_warns():
    assert gatekeep._floor_verdict(_decision("APPROVE", "LOW", "high")).verdict == "WARN"


def test_a_consistent_verdict_is_kept():
    for d in (_decision("APPROVE", "LOW", "MEDIUM"), _decision("WARN", "HIGH", "HIGH"), _decision("BLOCK", "CRITICAL")):
        assert gatekeep._floor_verdict(d) is d


def test_the_floor_never_lowers_a_verdict():
    assert gatekeep._floor_verdict(_decision("BLOCK", "LOW", "LOW")).verdict == "BLOCK"


def test_an_abstention_is_left_alone():
    d = _decision("UNAVAILABLE", "NONE", "CRITICAL")
    assert gatekeep._floor_verdict(d) is d


# -- Shadow mode: data-plane rules report "would block" but do not gate yet --------


def _typed(verdict, *issues):
    return gatekeep.GatekeepDecision(
        verdict=verdict,
        severity="CRITICAL",
        summary="s",
        issues=[{"type": t, "severity": s, "message": "m"} for t, s in issues],
        recommendation="r",
        provider="p",
    )


def test_a_block_resting_only_on_data_plane_is_held_back_and_says_so():
    d = gatekeep._shadow(gatekeep._floor_verdict(_typed("WARN", ("data_plane", "CRITICAL"), ("testing", "HIGH"))))
    assert (d.verdict, d.would_block) == ("WARN", True)
    assert gatekeep._exit_code(d.verdict) == 0
    assert "Would block (shadow mode)" in gatekeep._render_comment(d)


def test_any_critical_outside_the_shadow_still_blocks():
    d = gatekeep._shadow(_typed("BLOCK", ("data_plane", "CRITICAL"), ("security", "CRITICAL")))
    assert (d.verdict, d.would_block) == ("BLOCK", False)


def test_a_block_without_issues_is_not_attributed_to_the_shadow():
    d = _typed("BLOCK")
    assert gatekeep._shadow(d) is d


def test_non_block_verdicts_pass_through_the_shadow():
    for v in ("APPROVE", "WARN", "UNAVAILABLE"):
        d = _typed(v, ("data_plane", "CRITICAL"))
        assert gatekeep._shadow(d) is d
    assert "Would block" not in gatekeep._render_comment(_typed("WARN", ("style", "LOW")))


def test_shadow_reads_the_enumerated_rule_not_the_free_type():
    d = gatekeep.GatekeepDecision(
        verdict="BLOCK", severity="CRITICAL", summary="s", recommendation="r", provider="p",
        issues=[{"type": "correctness", "rule": "data_plane", "severity": "CRITICAL", "message": "m"}],
    )
    assert (gatekeep._shadow(d).verdict, gatekeep._shadow(d).would_block) == ("WARN", True)
    general = gatekeep.GatekeepDecision(
        verdict="BLOCK", severity="CRITICAL", summary="s", recommendation="r", provider="p",
        issues=[{"type": "data_plane", "rule": "general", "severity": "CRITICAL", "message": "m"}],
    )
    assert gatekeep._shadow(general).verdict == "BLOCK"  # an explicit rule wins over the type


def test_the_model_is_an_argument_first_then_env_then_default(monkeypatch):
    monkeypatch.delenv("BAIR_GATEKEEP_MODEL", raising=False)
    assert gatekeep._claude_model(None) == gatekeep._DEFAULT_CLAUDE_MODEL
    monkeypatch.setenv("BAIR_GATEKEEP_MODEL", "env-model")
    assert gatekeep._claude_model(None) == "env-model"
    assert gatekeep._claude_model("arg-model") == "arg-model"

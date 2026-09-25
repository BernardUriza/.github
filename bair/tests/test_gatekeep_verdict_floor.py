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

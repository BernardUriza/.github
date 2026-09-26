"""Response parsing must survive thinking blocks and stray prose.

Por qué existe (2026-09-26, eval run 36209279458): claude-opus-5-5 devuelve
bloques `thinking` antes del texto y `content[0]["text"]` tronó en 72/72 reviews;
claude-opus-4-7 falló 1/72 con `Extra data` por texto después del JSON.
"""

from __future__ import annotations

import pytest

from bair.pipelines import gatekeep

VERDICT = '{"verdict": "APPROVE", "severity": "LOW", "summary": "ok", "issues": [], "recommendation": ""}'


def test_text_is_read_past_leading_thinking_blocks():
    data = {"content": [{"type": "thinking", "thinking": "", "signature": "x"}, {"type": "text", "text": VERDICT}]}
    assert gatekeep._extract_json(gatekeep._response_text(data))["verdict"] == "APPROVE"


def test_a_response_without_text_raises_a_clear_error():
    with pytest.raises(ValueError, match="no text block"):
        gatekeep._response_text({"content": [{"type": "thinking", "thinking": ""}], "stop_reason": "max_tokens"})


@pytest.mark.parametrize(
    "raw",
    [
        VERDICT,
        f"```json\n{VERDICT}\n```",
        f"{VERDICT}\n\nNote: the diff also bumps the version.",
        f"Here is my review:\n{VERDICT}",
    ],
)
def test_the_first_json_object_is_read_despite_surrounding_prose(raw):
    assert gatekeep._extract_json(raw)["summary"] == "ok"


def test_no_object_raises():
    with pytest.raises(ValueError):
        gatekeep._extract_json("I could not review this.")


# -- _normalize: the model's JSON made safe for the code that gates on it ---------


def test_a_lowercase_block_still_exits_1():
    d = gatekeep._floor_verdict(gatekeep._normalize({"verdict": "block", "severity": "critical"}, provider="p"))
    assert (d.verdict, d.severity) == ("BLOCK", "CRITICAL")
    assert gatekeep._exit_code(d.verdict) == 1


def test_an_unknown_verdict_is_never_milder_than_warn():
    assert gatekeep._normalize({"verdict": "REQUEST_CHANGES", "severity": "LOW"}, provider="p").verdict == "WARN"
    crit = gatekeep._normalize({"verdict": "nope", "issues": [{"severity": "CRITICAL"}]}, provider="p")
    assert crit.verdict == "BLOCK"


def test_nulls_and_junk_issues_do_not_crash_the_gate():
    d = gatekeep._normalize({"verdict": "WARN", "severity": None, "issues": None, "summary": None}, provider="p")
    assert (d.severity, d.issues, d.summary) == ("MEDIUM", [], "")
    junk = gatekeep._normalize({"verdict": "APPROVE", "severity": "LOW", "issues": ["free text", None, {"severity": 5}]}, provider="p")
    assert junk.issues[0]["type"] == "unstructured" and len(junk.issues) == 2
    for step in (gatekeep._floor_verdict, gatekeep._shadow):
        step(junk)
    gatekeep._render_comment(junk)

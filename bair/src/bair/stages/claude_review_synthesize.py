"""claude_review_synthesize — second half of the adversarial review pair.

Plan v7 section 6b ("Variante review — sin commits, adversarial en logs").

This stage invokes Claude (the Anthropic side, claude-sonnet-4-6) over the
PR diff + the Codex draft from ``codex_review_generate``, and produces the
**final synthesized review** as a structured JSON payload that
``ReviewResponse.validate()`` can consume.

## What the synthesizer does

1. **Validates** each Codex finding against the actual source code using
   read-only tools (Read, Glob, Grep). Findings that don't survive the
   second perspective are dropped.
2. **Adds** findings that Claude saw but Codex missed (subtle semantic
   regressions, cross-file coupling, test coverage gaps).
3. **Produces** a single JSON object with the ReviewResponse shape
   (``title`` / ``summary`` / ``findings[]`` / ``highlights[]`` /
   ``knowledge``) so the existing publisher can consume it unchanged.

## Why two stages instead of one LLM call

Heterogeneity. A single-engine review cannot catch issues that the model
itself is blind to. Codex sees from GPT's distribution; Claude validates
from Anthropic's. Each finding has to survive **both perspectives** before
publication — that's what kills the calibration error the production
single-engine reviews ship today.

## Fallback when Codex unavailable

If ``prev["codex_review_generate"]["skipped"]`` is True or its outcome had
an error, the stage runs in **Claude-only mode**: same prompt without the
"validate Codex findings" framing, producing the review directly. The
adversarial value is lost for that run but the pipeline does not fail.

## Inputs (from prev)

- ``codex_review_generate.codex_draft``: the markdown draft from Codex
- ``codex_review_generate.skipped`` / ``succeeded``: gates fallback
- ``diff.diff``: the PR diff text
- ``rules.rules``: condensed coding rules
- ``context.*``: resolved threads, prior art, CSS health, CI status
- ``deep_analysis.deep_analysis``: optional Claude SDK deep analysis output

## Output

Dict with:

- ``synthesized_text`` (str): the raw text from the Claude run (for trace)
- ``synthesized_payload`` (dict): the parsed JSON payload ready for
  ``ReviewResponse.validate()``. Empty dict if parsing failed.
- ``outcome`` (AgentRunOutcome): full runner outcome for trace
- ``succeeded`` (bool): True iff outcome.succeeded AND payload parsed
- ``mode`` (str): ``"adversarial"`` or ``"claude_only"``
- ``parse_error`` (str | None): explanation if JSON parsing failed
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..contracts.agent_runner import AgentRunner
from ..domain.agent_run import AgentRunOutcome
from ..orchestration.stage import StageFn


_CLAUDE_SYNTHESIZE_SYSTEM_PROMPT = """\
You are Claude, the validating reviewer in an adversarial code review pipeline.

A first reviewer (Codex / GPT-5.5) has produced a draft review of this PR.
Your job: validate each Codex finding against the actual source code using
your Read/Glob/Grep tools, then produce the FINAL review.

## Your validation rules

1. **Drop hallucinated findings**: if Codex cites a file:line that doesn't
   exist or describes behavior the code doesn't have, drop it silently.
   The synthesizer prefers honest under-finding to fabricated over-finding.
2. **Keep validated findings**: when you confirm the finding against the
   source, keep it — but rewrite the message in your own voice.
3. **Add findings Codex missed**: if you spot a subtle semantic regression,
   cross-file coupling issue, or test coverage gap that Codex didn't see,
   add it.
4. **Recalibrate severity**: Codex tends to under-rate PII / security
   issues. If you see a CRITICAL finding marked WARNING in the draft,
   promote it.

## Output format — MUST be a single JSON object

Return a JSON object (no prose around it) with this shape:

```json
{
  "title": "Short title for the review",
  "summary": "1-3 sentence overview of the PR + the most important finding",
  "findings": [
    {
      "severity": "critical" | "warning" | "info",
      "file": "src/path/to/file.ts",
      "line": 42,
      "title": "Short title",
      "description": "What's wrong, why, and what to do about it.",
      "code_snippet": "optional — the offending line(s)"
    }
  ],
  "highlights": [
    "Optional positive observations — well-handled error case, good test."
  ],
  "knowledge": "Optional — short note for the team's learning log."
}
```

Severity rubric:
- **critical**: should block merge (security, data loss, broken invariants, PII leak)
- **warning**: should be addressed before merge but doesn't block
- **info**: stylistic / consistency observation

Empty arrays are fine. A clean PR returns ``{"title": ..., "summary": ...,
"findings": [], "highlights": [...], "knowledge": ""}``.

Wrap your JSON in a fenced code block like ```json ... ``` or output it
bare — the parser handles both. Do NOT add commentary before or after.
"""


def make_claude_review_synthesize_stage(
    runner: AgentRunner,
    *,
    system_prompt: str = _CLAUDE_SYNTHESIZE_SYSTEM_PROMPT,
    default_max_turns: int = 25,
) -> StageFn:
    """Factory: builds a stage function with the Claude runner closed over.

    The runner is injected so tests can substitute fakes. Production wires
    a real ``ClaudeSDKAgentRunner`` (with read-only tools — no Edit/Write/
    Bash for review) via the pipeline builder.

    Args:
        runner: any ``AgentRunner`` implementation
        system_prompt: synthesizer system prompt; uses the default distilled
            from plan v7. Override for experiments.
        default_max_turns: turns budget when ``ctx.max_turns`` not set.
            Bumped from 12 → 25 on 2026-05-19 after PR https://github.com/bernard-org/bernard-org-gen-backend/pull/1443
            silently dropped review findings: synthesizer exhausted 12 turns
            validating Codex findings across a 4-file PR, returned empty
            ``synthesized_text``, downstream ``generate_review`` failed and
            ``narrativize`` / ``publish`` skipped — zero visible output on
            the PR despite the run conclusion being ``success``. 25 gives
            enough headroom for typical multi-file PRs without runaway cost.
    """

    def claude_review_synthesize(ctx: Any, prev: dict[str, Any]) -> dict[str, Any]:
        """Invoke Claude over the diff + Codex draft, return final review payload."""
        codex_out = prev.get("codex_review_generate", {}) or {}
        codex_draft = codex_out.get("codex_draft", "") if isinstance(codex_out, dict) else ""
        codex_skipped = bool(codex_out.get("skipped")) if isinstance(codex_out, dict) else False
        codex_failed = (
            not bool(codex_out.get("succeeded", True)) if isinstance(codex_out, dict) else True
        )

        mode = "adversarial"
        if codex_skipped or codex_failed or not codex_draft:
            mode = "claude_only"

        user_prompt = _build_synthesize_user_prompt(prev, codex_draft=codex_draft, mode=mode)
        if not user_prompt:
            # No diff to review — skip stage gracefully
            return {
                "synthesized_text": "",
                "synthesized_payload": {},
                "outcome": AgentRunOutcome(),
                "succeeded": False,
                "skipped": True,
                "mode": mode,
                "parse_error": "no diff available",
            }

        cwd = _extract_workspace(ctx)
        max_turns = _extract_max_turns(ctx, default=default_max_turns)

        raw_outcome = runner.run(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            cwd=cwd,
            max_turns=max_turns,
        )
        if not isinstance(raw_outcome, AgentRunOutcome):
            raise TypeError(
                f"AgentRunner.run() returned {type(raw_outcome).__name__}, "
                "expected AgentRunOutcome (contract violation)."
            )

        outcome: AgentRunOutcome = raw_outcome
        payload, parse_error = _parse_synthesized_json(outcome.result_text)

        return {
            "synthesized_text": outcome.result_text,
            "synthesized_payload": payload,
            "outcome": outcome,
            "succeeded": outcome.succeeded and parse_error is None,
            "skipped": False,
            "mode": mode,
            "turns": outcome.turns,
            "tool_calls": outcome.tool_calls,
            "error": outcome.error,
            "parse_error": parse_error,
        }

    return claude_review_synthesize


# -- Helpers --------------------------------------------------------------


def _build_synthesize_user_prompt(
    prev: dict[str, Any], *, codex_draft: str, mode: str
) -> str:
    """Assemble the synthesizer user prompt.

    In ``adversarial`` mode the Codex draft is the centerpiece — Claude
    validates against it. In ``claude_only`` mode the draft section is
    omitted and Claude produces the review from scratch.

    Returns "" if no diff is available — the stage will skip.
    """
    diff_out = prev.get("diff", {})
    diff_text = diff_out.get("diff", "") if isinstance(diff_out, dict) else ""
    if not diff_text:
        return ""

    rules_out = prev.get("rules", {})
    rules = rules_out.get("rules", "") if isinstance(rules_out, dict) else ""

    context_out = prev.get("context", {})
    if isinstance(context_out, dict):
        resolved_threads = context_out.get("resolved_threads", "")
        prior_art = context_out.get("prior_art", "")
        css_health = context_out.get("css_health", "")
        ci_status = context_out.get("ci_status", "")
    else:
        resolved_threads = prior_art = css_health = ci_status = ""

    deep_out = prev.get("deep_analysis", {})
    deep_analysis = deep_out.get("deep_analysis", "") if isinstance(deep_out, dict) else ""

    sections = ["## PR Diff", "", diff_text, ""]

    if mode == "adversarial" and codex_draft:
        sections += [
            "## Codex Draft Review (validate this against the source)",
            "",
            codex_draft,
            "",
        ]
    elif mode == "claude_only":
        sections += [
            "## Note",
            "",
            "Codex draft was unavailable for this run. Produce the review "
            "from scratch using only the diff + context below.",
            "",
        ]

    if rules:
        sections += ["## Coding Rules (condensed)", "", rules, ""]
    if resolved_threads:
        sections += ["## Resolved Threads (prior review history)", "", resolved_threads, ""]
    if prior_art:
        sections += ["## Prior Art (similar past code)", "", prior_art, ""]
    if css_health:
        sections += ["## CSS Health", "", css_health, ""]
    if ci_status:
        sections += ["## CI / Test Status", "", ci_status, ""]
    if deep_analysis:
        sections += ["## Deep Analysis (Claude SDK earlier in pipeline)", "", deep_analysis, ""]

    sections += [
        "## Task",
        "",
        "Produce the final review JSON payload as described in the system "
        "prompt. Validate Codex findings against the actual source via "
        "your Read/Glob/Grep tools before keeping them.",
    ]
    return "\n".join(sections)


_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_synthesized_json(text: str) -> tuple[dict[str, Any], str | None]:
    """Parse the synthesizer output into a ReviewResponse-shaped dict.

    Tries fenced ```json block first, then bare JSON object scanning. Returns
    ``({}, error_message)`` on parse failure so the caller can log and the
    publisher can short-circuit to an empty ReviewResponse.
    """
    if not text or not text.strip():
        return {}, "empty synthesizer output"

    # Try fenced block first
    m = _FENCED_JSON_RE.search(text)
    if m:
        candidate = m.group(1)
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data, None
        except json.JSONDecodeError as e:
            return {}, f"fenced JSON parse error: {e}"

    # Fallback: find first balanced {...} block
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data, None
        except json.JSONDecodeError as e:
            return {}, f"bare JSON parse error: {e}"

    return {}, "no JSON object found in synthesizer output"


def _extract_workspace(ctx: Any) -> str:
    """Resolve cwd for the synthesizer agent.

    Order:
    1. ``ctx.workspace`` if set explicitly by the pipeline caller.
    2. ``TARGET_REPO_PATH`` env (set by ai-dispatch.yml workflow to
       ``${{ github.workspace }}/target-repo``) — without this, the
       agent runs with cwd="." which in CI is the runner workdir, not
       the checked-out PR repo, and the agent burns minutes searching
       /root, /tmp, /home for source files before finding them.
    3. ``.`` fallback for local runs where no env is set.
    """
    import os
    if hasattr(ctx, "workspace") and isinstance(ctx.workspace, str):
        return ctx.workspace
    env_path = os.environ.get("TARGET_REPO_PATH", "").strip()
    if env_path:
        return env_path
    return "."


def _extract_max_turns(ctx: Any, *, default: int) -> int:
    if hasattr(ctx, "max_turns"):
        v = ctx.max_turns
        if isinstance(v, int) and v > 0:
            return v
    return default

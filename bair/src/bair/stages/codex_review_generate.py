"""codex_review_generate — first half of the adversarial review pair.

Plan v7 section 6b ("Variante review — sin commits, adversarial en logs").

This stage invokes Codex (the OpenAI side, gpt-5.5) over the PR diff +
gathered context, and produces a **draft review as structured markdown**.
The draft is the input to the next stage, ``claude_review_synthesize``,
where Claude (the Anthropic side) validates each finding against the actual
source code and produces the final published review.

## Why two stages instead of one LLM call

Heterogeneity is the entire point. A single-engine review (the current
production behavior) cannot catch issues that the model itself is blind to.
Codex sees the diff from GPT's training distribution; Claude validates from
Anthropic's. Each finding has to survive **both perspectives** to make it
into the final review — that's what kills the calibration error (e.g. the
"PII leak as LOW-MED" miscall that BAIR caught when reviewing my manual
review of PR #1346).

## Inputs (from prev)

- ``diff.diff``: the PR diff text
- ``rules.rules``: condensed coding rules
- ``context.resolved_threads`` / ``prior_art`` / ``css_health`` / ``ci_status``:
  context bundle from gatherers
- ``deep_analysis.deep_analysis``: optional Claude SDK deep analysis output

## Inputs (from ctx)

- ``agent_runner``: a ``CodexCLIAgentRunner`` (or fake) implementing the
  ``AgentRunner`` Protocol
- ``max_turns``: Codex tool-use turn limit (default 8 — review is faster
  than work, fewer turns needed)
- ``system_prompt``: review-specific system prompt (overridable for tests)

## Output

Dict with:

- ``codex_draft`` (str): the markdown review draft from Codex
- ``outcome`` (AgentRunOutcome): full runner outcome for trace
- ``succeeded`` (bool): convenience flag for downstream gating

## Failure policy

``on_failure="continue"`` recommended at the pipeline level. If Codex
fails, the synthesizer can still produce a Claude-only review using the
diff/rules/context. The adversarial value is lost, but the run isn't.
"""

from __future__ import annotations

from typing import Any

from ..contracts.agent_runner import AgentRunner
from ..domain.agent_run import AgentRunOutcome
from ..orchestration.stage import StageFn


# Default system prompt for the Codex review generation pass.
# Distilled from the plan v7 sections on what the Codex perspective should produce.
_CODEX_REVIEW_SYSTEM_PROMPT = """\
You are Codex, the GPT-5.5 reviewer in an adversarial code review pipeline.

Your job: read the PR diff + the surrounding context, and produce a structured
review draft that another reviewer (Claude) will later validate against the
actual source. Your draft is NOT the final review — it's a hypothesis space.

## Output format

Produce markdown with sections:

### Critical
Issues that should block merge (security, data loss, broken invariants, PII).

### Warnings
Issues that should be addressed before merge but don't block.

### Info
Stylistic / consistency observations. The reviewer may keep or drop.

### Highlights
Specific code that's clearly correct + worth noting (e.g., careful error handling).

Every finding MUST cite file:line. Findings without anchors are useless to
the synthesizer.

## What you do well (GPT strengths)

- Spotting type errors and obvious logic bugs
- Detecting common security anti-patterns (SQL injection, command injection, etc.)
- Noticing missing error handling

## What you might miss (and Claude will catch)

- Subtle semantic regressions ("this used to do X, now does Y")
- Cross-file coupling that requires reading multiple modules
- Test coverage gaps for behavioral changes

Don't pretend to find things you didn't. Empty sections are fine. The
synthesizer prefers honest under-finding to fabricated over-finding.
"""


def make_codex_review_generate_stage(
    runner: AgentRunner,
    *,
    system_prompt: str = _CODEX_REVIEW_SYSTEM_PROMPT,
    default_max_turns: int = 8,
) -> StageFn:
    """Factory: builds a stage function with the Codex runner closed over.

    The runner is injected so tests can substitute fakes. Production wires
    a real ``CodexCLIAgentRunner`` via the pipeline builder.

    Args:
        runner: any ``AgentRunner`` implementation
        system_prompt: review-specific system prompt; uses the default
            distilled from plan v7. Override for experiments.
        default_max_turns: turns budget when ``ctx.max_turns`` not set
    """

    def codex_review_generate(ctx: Any, prev: dict[str, Any]) -> dict[str, Any]:
        """Invoke Codex over the PR diff + context, return draft review."""
        user_prompt = _build_codex_user_prompt(prev)
        if not user_prompt:
            # No diff to review — skip stage gracefully
            return {
                "codex_draft": "",
                "outcome": AgentRunOutcome(),
                "succeeded": False,
                "skipped": True,
                "reason": "no diff available",
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
        return {
            "codex_draft": outcome.result_text,
            "outcome": outcome,
            "succeeded": outcome.succeeded,
            "skipped": False,
            "turns": outcome.turns,
            "tool_calls": outcome.tool_calls,
            "error": outcome.error,
        }

    return codex_review_generate


# -- Helpers --------------------------------------------------------------


def _build_codex_user_prompt(prev: dict[str, Any]) -> str:
    """Assemble the user prompt from upstream stage outputs.

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
        sections += ["## Deep Analysis (Claude SDK)", "", deep_analysis, ""]

    sections += [
        "## Task",
        "",
        "Produce your review draft in the format described in the system prompt.",
        "Cite file:line for every finding. Empty sections are fine.",
    ]
    return "\n".join(sections)


def _extract_workspace(ctx: Any) -> str:
    """Codex needs a cwd to run tool-use against the cloned repo."""
    if hasattr(ctx, "workspace") and isinstance(ctx.workspace, str):
        return ctx.workspace
    # ReviewBridgeContext doesn't have workspace by default — fall back to "."
    return "."


def _extract_max_turns(ctx: Any, *, default: int) -> int:
    if hasattr(ctx, "max_turns"):
        v = ctx.max_turns
        if isinstance(v, int) and v > 0:
            return v
    return default

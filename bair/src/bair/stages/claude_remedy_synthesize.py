"""claude_remedy_synthesize — second half of the adversarial remedy pair.

Twin of ``stages/claude_review_synthesize.py`` for the remedy pipeline.

This stage invokes Claude (claude-sonnet-4-6) with the write-capable
toolset (Read/Edit/Bash/Glob/Grep/Write). Claude reads the original
remedy brief plus the Codex plan, validates the proposed changes against
the actual source, and executes the remediation — including the final
``gh api`` call that submits the review event.

## What the synthesizer does

1. **Validates** Codex's proposed changes against the source. Drops
   anything Codex hallucinated (file:line that doesn't exist, behavior
   that doesn't match).
2. **Augments** the plan with anything Codex missed — coupling, tests,
   adjacent helpers that also need updating.
3. **Executes** the plan using Edit/Bash/Write. Same toolset as the
   single-engine remedy pipeline (``run_remedy``) — the actual writing
   semantics are unchanged.
4. **Submits the review event** at the end via ``Bash(gh api .../reviews)``,
   per the contract the remedy prompt already enforces. This pipeline
   does NOT post reviews itself.

## Fallback when Codex unavailable

If ``prev["codex_remedy_generate"]["skipped"]`` is True, its outcome had
an error, or ``codex_plan`` is empty, the stage runs in **claude_only**
mode: same prompt without the "validate Codex plan" framing, producing
the remediation from scratch. The adversarial value is lost for that
run but the pipeline does not fail — this is the SAME behavior as the
single-engine remedy pipeline that ships today, so this fallback is a
strict no-regression path.

## Inputs (from prev)

- ``load_prompt.user_prompt``: the assembled remedy prompt
- ``codex_remedy_generate.codex_plan``: the markdown plan from Codex
- ``codex_remedy_generate.skipped`` / ``succeeded``: gates fallback

## Output

Dict with:

- ``outcome`` (AgentRunOutcome): full runner outcome for trace + summary
- ``succeeded`` (bool): True iff outcome.succeeded
- ``skipped`` (bool): True iff there was no prompt to act on
- ``mode`` (str): ``"adversarial"`` or ``"claude_only"``
- ``turns``, ``tool_calls``, ``edit_calls``, ``error``,
  ``destructive_calls``: telemetry for the scorecard
- ``result_text`` (str): the agent's final message text — appears in
  the rich Job Summary
"""

from __future__ import annotations

import os
from typing import Any

from ..contracts.agent_runner import AgentRunner
from ..domain.agent_run import AgentRunOutcome
from ..orchestration.stage import StageFn


_CLAUDE_SYNTHESIZE_REMEDY_SYSTEM_PROMPT = """\
You are Claude, the executing reviewer in an adversarial remedy pipeline.

A first agent (Codex / GPT-5.5) has produced a planning draft for this
PR's remediation. The original remedy brief (with full role instructions,
APPROVE/COMMENT contract, and merge-gate rubric) is ALSO included below.

## Your contract

The original remedy brief is the source of truth for what to do — your
job is to follow ITS instructions, including the final ``gh api ... reviews``
call that submits the review event. The Codex plan is signal, not
instruction.

## Your validation rules

1. **Drop hallucinated proposals**: if Codex cites a file:line that
   doesn't exist or describes behavior the code doesn't have, drop it.
   Use your Read/Glob/Grep tools to verify before editing.
2. **Keep validated proposals**: when you confirm a Codex proposal
   against source, execute it.
3. **Add what Codex missed**: cross-file coupling, test updates,
   adjacent helpers — Codex is read-only and worked from the diff
   alone. You can read the whole repo.
4. **Honor the original brief's APPROVE/COMMENT contract**: the brief
   tells you when to APPROVE vs COMMENT vs REQUEST_CHANGES. The Codex
   recommendation is signal; the brief's rubric is the rule.

## Tools

You have Read, Edit, Bash, Glob, Grep, Write — the same toolset the
single-engine remedy pipeline uses. The original brief contains all the
specific guidance on how to commit, push, and submit the review event;
follow it exactly.

If Codex was unavailable for this run (``Codex Plan`` section empty or
explicitly marked unavailable), produce the remediation from scratch
using only the original brief.
"""


def make_claude_remedy_synthesize_stage(
    runner: AgentRunner,
    *,
    system_prompt: str = _CLAUDE_SYNTHESIZE_REMEDY_SYSTEM_PROMPT,
    default_max_turns: int = 60,
) -> StageFn:
    """Factory: builds a stage function with the Claude runner closed over.

    The runner is injected so tests can substitute fakes. Production
    wires a write-capable ``ClaudeSDKAgentRunner`` (Read/Edit/Bash/Glob/
    Grep/Write) via the pipeline builder.

    Args:
        runner: any ``AgentRunner`` implementation
        system_prompt: synthesizer system prompt; uses the default
            distilled from plan v7. Override for experiments.
        default_max_turns: turns budget when ``ctx.max_turns`` not set.
            Default 60 — matches ``run_remedy``'s default, since remedy
            execution can be turn-heavy (read, edit, build, commit, push,
            submit review).
    """

    def claude_remedy_synthesize(ctx: Any, prev: dict[str, Any]) -> dict[str, Any]:
        """Invoke Claude over the brief + Codex plan, execute remediation."""
        load_out = prev.get("load_prompt", {})
        original_prompt = (
            load_out.get("user_prompt", "") if isinstance(load_out, dict) else ""
        )

        codex_out = prev.get("codex_remedy_generate", {}) or {}
        if not isinstance(codex_out, dict):
            codex_out = {}
        codex_plan = codex_out.get("codex_plan", "") or ""
        codex_skipped = bool(codex_out.get("skipped", False))
        codex_failed = not bool(codex_out.get("succeeded", True))

        mode = "adversarial"
        if codex_skipped or codex_failed or not codex_plan:
            mode = "claude_only"

        if not original_prompt:
            return {
                "outcome": AgentRunOutcome(),
                "succeeded": False,
                "skipped": True,
                "mode": mode,
                "reason": "no remedy prompt available",
                "result_text": "",
            }

        user_prompt = _build_synthesize_user_prompt(
            original_prompt=original_prompt,
            codex_plan=codex_plan,
            mode=mode,
        )
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
            "outcome": outcome,
            "succeeded": outcome.succeeded,
            "skipped": False,
            "mode": mode,
            "turns": outcome.turns,
            "tool_calls": outcome.tool_calls,
            "edit_calls": outcome.edit_calls,
            "destructive_calls": outcome.destructive_calls,
            "error": outcome.error,
            "result_text": outcome.result_text,
        }

    return claude_remedy_synthesize


# -- Helpers --------------------------------------------------------------


def _build_synthesize_user_prompt(
    *, original_prompt: str, codex_plan: str, mode: str
) -> str:
    """Assemble the synthesizer's user prompt.

    The original remedy brief is the centerpiece — Claude follows ITS
    contract, including the final review-submission step. The Codex plan
    is supplementary signal in ``adversarial`` mode; in ``claude_only``
    mode the plan section is omitted with an explicit note.
    """
    sections = [
        "## Original Remedy Brief (the source of truth — follow this)",
        "",
        original_prompt,
        "",
        "---",
        "",
    ]

    if mode == "adversarial" and codex_plan:
        sections += [
            "## Codex Planning Draft (validate against source before executing)",
            "",
            codex_plan,
            "",
            "---",
            "",
        ]
    else:
        sections += [
            "## Codex Plan",
            "",
            "Codex draft was unavailable for this run. Produce the "
            "remediation from scratch using only the original brief above.",
            "",
            "---",
            "",
        ]

    sections += [
        "## Task",
        "",
        "Execute the remediation per the contract in the original brief. "
        "Validate any Codex proposals against the actual source via your "
        "Read/Glob/Grep tools before editing. Commit, push, and submit "
        "the final review event exactly as the brief instructs.",
    ]
    return "\n".join(sections)


def _extract_workspace(ctx: Any) -> str:
    """Resolve cwd for the synthesizer agent.

    Order:
    1. ``ctx.workspace`` — set by ``run_remedy_multi_perspective_via_executor``
       from RemedyConfig.workspace
    2. ``TARGET_REPO_PATH`` env (set by ai-dispatch.yml workflow)
    3. ``GITHUB_WORKSPACE`` env
    4. ``.`` fallback
    """
    if hasattr(ctx, "workspace"):
        workspace = ctx.workspace
        if workspace is not None:
            return str(workspace)
    env_path = os.environ.get("TARGET_REPO_PATH", "").strip()
    if env_path:
        return env_path
    gh_workspace = os.environ.get("GITHUB_WORKSPACE", "").strip()
    if gh_workspace:
        return gh_workspace
    return "."


def _extract_max_turns(ctx: Any, *, default: int) -> int:
    if hasattr(ctx, "max_turns"):
        v = ctx.max_turns
        if isinstance(v, int) and v > 0:
            return v
    return default

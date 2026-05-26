"""claude_resolve_synthesize — second half of the adversarial work pair.

Twin of ``stages/claude_review_synthesize.py`` and
``stages/claude_remedy_synthesize.py`` for the resolve pipeline.

This stage invokes Claude (claude-sonnet-4-6) with the write-capable
toolset (Read/Edit/Bash/Glob/Grep/Write). Claude reads the original work
brief plus the Codex plan, validates the proposed changes against the
actual source, and executes the resolution — Edit/Write to source,
Bash for builds/tests/git.

## What the synthesizer does

1. **Validates** Codex's proposed changes against the source. Drops
   anything Codex hallucinated (file:line that doesn't exist, behavior
   that doesn't match).
2. **Augments** the plan with anything Codex missed — coupling, tests,
   adjacent helpers that also need updating.
3. **Executes** the plan using Edit/Bash/Write. Same toolset as the
   single-engine resolve pipeline (``run_resolve_full_via_executor``) — the
   actual writing semantics are unchanged.
4. **Stops short of pushing / PR creation**. Unlike remedy (which calls
   ``gh api ... reviews`` at the end), work's downstream stages
   (``substance_gate``, ``commit_push``, ``verify_build``,
   ``create_pr_stage``, ``update_tracker``) are reused as-is from
   ``stages/resolve_postagent.py``. The synthesizer's job ends when source
   is modified and (optionally) committed locally; the pipeline does the
   rest.

## Fallback when Codex unavailable

If ``prev["codex_resolve_generate"]["skipped"]`` is True, its outcome had
an error, or ``codex_plan`` is empty, the stage runs in **claude_only**
mode: same prompt without the "validate Codex plan" framing, producing
the resolution from scratch. The adversarial value is lost for that
run but the pipeline does not fail — the runner config and toolset are
identical to the single-engine path, so this fallback is a strict
no-regression invariant.

## Inputs (from prev)

- ``build_prompt.prompt``: the assembled work prompt
- ``codex_resolve_generate.codex_plan``: the markdown plan from Codex
- ``codex_resolve_generate.skipped`` / ``succeeded``: gates fallback

## Output shape

Mirrors ``stages/agent_run.py:make_agent_run_stage`` exactly — so the
downstream post-agent stages (``assert_agent_succeeded``,
``substance_gate``, ``commit_push``, etc.) consume this stage's output
under either name. The multi-perspective pipeline aliases this stage's
output to ``agent_run`` in the DAG so reuse is mechanical.

Dict with:

- ``outcome`` (AgentRunOutcome): full runner outcome for trace + summary
- ``succeeded`` (bool): True iff outcome.succeeded
- ``skipped`` (bool): True iff there was no prompt to act on
- ``mode`` (str): ``"adversarial"`` or ``"claude_only"``
- ``turns``, ``tool_calls``, ``edit_calls``, ``has_substance``,
  ``error``, ``destructive_calls``, ``result_text``: same shape as
  ``agent_run`` output (consumed by post-agent stages)
"""

from __future__ import annotations

import os
from typing import Any

from ..contracts.agent_runner import AgentRunner
from ..domain.agent_run import AgentRunOutcome
from ..orchestration.stage import StageFn


_CLAUDE_SYNTHESIZE_WORK_SYSTEM_PROMPT = """\
You are Claude, the executing engineer in an adversarial resolve pipeline.

A first agent (Codex / GPT-5.5) has produced a planning draft for this
issue's resolution. The original work brief (with full role instructions
and the issue context) is ALSO included below.

## Your contract

The original work brief is the source of truth for what to do — your job
is to follow ITS instructions. The Codex plan is signal, not instruction.

## Your validation rules

1. **Drop hallucinated proposals**: if Codex cites a file:line that
   doesn't exist or describes behavior the code doesn't have, drop it.
   Use your Read/Glob/Grep tools to verify before editing.
2. **Keep validated proposals**: when you confirm a Codex proposal
   against source, execute it.
3. **Add what Codex missed**: cross-file coupling, test updates,
   adjacent helpers — Codex is read-only and worked from the brief
   alone. You can read the whole repo.
4. **Honor the original brief's contract**: the brief tells you how to
   work this issue (what files, what tests, what gates). The Codex
   recommendation is signal; the brief is the rule.

## Tools

You have Read, Edit, Bash, Glob, Grep, Write — the same toolset the
single-engine resolve pipeline uses. The original brief contains all the
specific guidance on how to make the changes; follow it exactly.

If Codex was unavailable for this run (``Codex Plan`` section empty or
explicitly marked unavailable), produce the resolution from scratch
using only the original brief.

## What you do NOT do

Pushing, opening the PR, and updating the issue tracker happen in
downstream pipeline stages — NOT inside your tool loop. Stop when
source has been modified and (if you committed locally) the commit is
ready to push. The pipeline does the rest.
"""


def make_claude_resolve_synthesize_stage(
    runner: AgentRunner,
    *,
    system_prompt: str = _CLAUDE_SYNTHESIZE_WORK_SYSTEM_PROMPT,
    default_max_turns: int = 12,
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
            Default 12 — matches ``ResolveConfig.max_agent_turns`` historical
            default, since work execution can be turn-heavy (read, edit,
            build, fix, repeat).
    """

    def claude_resolve_synthesize(ctx: Any, prev: dict[str, Any]) -> dict[str, Any]:
        """Invoke Claude over the brief + Codex plan, execute resolution."""
        build_prompt_out = prev.get("build_prompt", {})
        original_prompt = (
            build_prompt_out.get("prompt", "")
            if isinstance(build_prompt_out, dict)
            else ""
        )

        codex_out = prev.get("codex_resolve_generate", {}) or {}
        if not isinstance(codex_out, dict):
            codex_out = {}
        codex_plan = codex_out.get("codex_plan", "") or ""
        codex_skipped = bool(codex_out.get("skipped", False))
        codex_failed = not bool(codex_out.get("succeeded", True))

        mode = "adversarial"
        if codex_skipped or codex_failed or not codex_plan:
            mode = "claude_only"

        if not original_prompt:
            empty_outcome = AgentRunOutcome()
            return {
                "outcome": empty_outcome,
                "succeeded": False,
                "skipped": True,
                "mode": mode,
                "reason": "no work prompt available",
                # Fields mirrored from make_agent_run_stage output shape so
                # downstream post-agent stages can consume this dict under
                # the "agent_run" alias.
                "turns": 0,
                "tool_calls": 0,
                "edit_calls": 0,
                "has_substance": False,
                "result_text": "",
                "destructive_calls": (),
                "error": None,
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
            # Fields mirrored from make_agent_run_stage so this dict is a
            # drop-in for ``prev["agent_run"]`` consumed by
            # ``assert_agent_succeeded``, ``substance_gate``, etc.
            "turns": outcome.turns,
            "tool_calls": outcome.tool_calls,
            "edit_calls": outcome.edit_calls,
            "has_substance": outcome.has_substance,
            "result_text": outcome.result_text,
            "destructive_calls": outcome.destructive_calls,
            "error": outcome.error,
        }

    return claude_resolve_synthesize


# -- Helpers --------------------------------------------------------------


def _build_synthesize_user_prompt(
    *, original_prompt: str, codex_plan: str, mode: str
) -> str:
    """Assemble the synthesizer's user prompt.

    The original work brief is the centerpiece — Claude follows ITS
    contract. The Codex plan is supplementary signal in ``adversarial``
    mode; in ``claude_only`` mode the plan section is omitted with an
    explicit note so the synthesizer doesn't try to grep for a missing
    section.
    """
    sections = [
        "## Original Work Brief (the source of truth — follow this)",
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
            "resolution from scratch using only the original brief above.",
            "",
            "---",
            "",
        ]

    sections += [
        "## Task",
        "",
        "Execute the resolution per the contract in the original brief. "
        "Validate any Codex proposals against the actual source via your "
        "Read/Glob/Grep tools before editing. Stop when source has been "
        "modified — the pipeline handles substance-gating, build verify, "
        "commit/push, and PR creation in downstream stages.",
    ]
    return "\n".join(sections)


def _extract_workspace(ctx: Any) -> str:
    """Resolve cwd for the synthesizer agent.

    Order:
    1. ``ctx.workspace`` — set by the pipeline runner from WORKSPACE env
    2. ``WORKSPACE`` env
    3. ``GITHUB_WORKSPACE`` env
    4. ``.`` fallback
    """
    if hasattr(ctx, "workspace"):
        workspace = ctx.workspace
        if workspace:
            return str(workspace)
    env_path = os.environ.get("WORKSPACE", "").strip()
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

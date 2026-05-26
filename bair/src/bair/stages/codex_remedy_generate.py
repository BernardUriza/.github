"""codex_remedy_generate — first half of the adversarial remedy pair.

Twin of ``stages/codex_review_generate.py`` for the remedy pipeline.

Where review's Codex produces a draft review (findings to validate), remedy's
Codex produces a **draft remediation plan**: an analysis of the PR review
comments + diff state, followed by a concrete list of files/changes the
synthesizer should make. The synthesizer (Claude, with Edit/Bash/Write
tools) then validates the plan against source and executes it.

## Why two stages instead of one LLM call

Same heterogeneity argument as review (see plan v7 §6b). A single-engine
remedy is blind to whatever its own training distribution misses. Codex
drafts from GPT's perspective; Claude validates from Anthropic's. Each
proposed change has to survive both lenses before code is written.

## What this stage produces

Codex is run **read-only** here — it has no Edit/Write/Bash. It reads the
remedy prompt artifact (already assembled by ``claude_remedy_builder.py``)
plus the PR diff state, and emits a markdown plan with sections:

    ### Analysis
    What the reviewers want, what the current code does, what the gap is.

    ### Proposed changes
    file:line + "what to change and why", grouped by file.

    ### Open questions
    Things Codex isn't sure about — the synthesizer should resolve via
    Read/Glob/Grep before editing.

    ### Approve-or-comment recommendation
    If the PR is clean, say so. The synthesizer makes the final call.

## Inputs (from prev)

- ``load_prompt.user_prompt``: the assembled remedy prompt from the
  artifact written by dispatch.py
- ``load_prompt.repo`` / ``pr_number``: convenience refs for prompt

## Inputs (from ctx)

- ``agent_runner``: a ``CodexCLIAgentRunner`` (or fake) implementing the
  ``AgentRunner`` Protocol
- ``max_turns``: Codex tool-use turn limit (default 8 — remedy planning
  doesn't need many turns; the synthesizer does the heavy work)
- ``workspace``: cwd for the agent (the cloned PR repo)

## Output

Dict with:

- ``codex_plan`` (str): the markdown plan from Codex
- ``outcome`` (AgentRunOutcome): full runner outcome for trace
- ``succeeded`` (bool): convenience flag for downstream gating
- ``skipped`` (bool): True iff there was no prompt to act on
- ``turns``, ``tool_calls``, ``error``: telemetry for the scorecard

## Failure policy

``on_failure="continue"`` recommended at the pipeline level. If Codex
fails, the synthesizer falls back to ``claude_only`` mode — same agent
that runs in the single-engine remedy pipeline today, no regression.
"""

from __future__ import annotations

import os
from typing import Any

from ..contracts.agent_runner import AgentRunner
from ..domain.agent_run import AgentRunOutcome
from ..orchestration.stage import StageFn


_CODEX_REMEDY_SYSTEM_PROMPT = """\
You are Codex, the GPT-5.5 planner in an adversarial remedy pipeline.

Another agent (Claude) will execute the remediation: write files, run
commands, commit, push, and submit the PR review event. Your job is to
read the PR + the reviewer comments and produce the **plan** Claude will
follow.

You are READ-ONLY. You can grep and read code but you cannot edit, run
commands, or push. Do not propose to do those things yourself — propose
them as instructions to Claude.

## Output format

Produce markdown with sections:

### Analysis
1-3 paragraphs: what do the reviewers want, what does the current code
do, where is the gap? Cite file:line where relevant.

### Proposed changes
A bulleted list grouped by file:

- `path/to/file.ts`
  - `line 42`: change X to Y because Z.
  - `line 87`: add error handling for the empty-array case.

Be specific. "Refactor for clarity" is useless. The synthesizer needs
enough detail to make the edit without re-reading every line.

### Open questions
Things you're not sure about. Claude has Read/Glob/Grep and can resolve
these before editing. Don't fabricate — flag uncertainty.

### Approve-or-comment recommendation
One of:
- **APPROVE** — the PR is clean, no changes needed. Justify in 1-2 lines.
- **COMMENT** — changes are needed but reviewer noise only, no merge gate.
- **REQUEST_CHANGES** — changes are required, list the merge gates.

Claude makes the final call (it has the contract with the workflow). Your
recommendation is signal, not decision.

## What you do well (GPT strengths)

- Spotting the literal "fix this typo" / "rename this" comments and
  staging the mechanical edits
- Identifying missing error paths and obvious null checks
- Spotting calls to deprecated APIs that the reviewer flagged

## What you might miss (and Claude will catch)

- Cross-file ripple effects of the proposed edit
- Test files that need updating alongside the source change
- Whether the change conflicts with a recent refactor in the same area

Empty sections are fine. Honest under-planning beats fabricated
over-planning — the synthesizer will read source anyway.
"""


def make_codex_remedy_generate_stage(
    runner: AgentRunner,
    *,
    system_prompt: str = _CODEX_REMEDY_SYSTEM_PROMPT,
    default_max_turns: int = 8,
) -> StageFn:
    """Factory: builds a stage function with the Codex runner closed over.

    The runner is injected so tests can substitute fakes. Production wires
    a real ``CodexCLIAgentRunner`` via the pipeline builder.

    Args:
        runner: any ``AgentRunner`` implementation
        system_prompt: remedy-planner system prompt; uses the default
            distilled from plan v7. Override for experiments.
        default_max_turns: turns budget when ``ctx.max_turns`` not set.
            Default 8 — planning is faster than execution; synthesizer
            gets the higher budget.
    """

    def codex_remedy_generate(ctx: Any, prev: dict[str, Any]) -> dict[str, Any]:
        """Invoke Codex over the remedy prompt + diff, return draft plan."""
        load_out = prev.get("load_prompt", {})
        user_prompt = (
            load_out.get("user_prompt", "") if isinstance(load_out, dict) else ""
        )
        if not user_prompt:
            # Nothing to plan against — skip stage gracefully so the
            # synthesizer can fall back to claude_only mode.
            return {
                "codex_plan": "",
                "outcome": AgentRunOutcome(),
                "succeeded": False,
                "skipped": True,
                "reason": "no remedy prompt available",
            }

        codex_prompt = _build_codex_user_prompt(user_prompt)
        cwd = _extract_workspace(ctx)
        max_turns = _extract_max_turns(ctx, default=default_max_turns)

        raw_outcome = runner.run(
            user_prompt=codex_prompt,
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
            "codex_plan": outcome.result_text,
            "outcome": outcome,
            "succeeded": outcome.succeeded,
            "skipped": False,
            "turns": outcome.turns,
            "tool_calls": outcome.tool_calls,
            "error": outcome.error,
        }

    return codex_remedy_generate


# -- Helpers --------------------------------------------------------------


def _build_codex_user_prompt(remedy_prompt: str) -> str:
    """Wrap the assembled remedy prompt in planner framing.

    The remedy prompt itself already contains: the PR metadata, the diff,
    the reviewer comments, the merge-gate rubric, and the APPROVE/COMMENT
    contract. We don't re-assemble any of that — Codex reads it as-is and
    produces the plan section that Claude will follow.
    """
    return (
        "## Original Remedy Brief (assembled by dispatch)\n"
        "\n"
        f"{remedy_prompt}\n"
        "\n"
        "---\n"
        "\n"
        "## Your Task\n"
        "\n"
        "Read the brief above, then produce your planning markdown in the "
        "format described in the system prompt. You are READ-ONLY — propose "
        "changes for Claude (the synthesizer) to execute. Do not attempt to "
        "edit files, run commands, or post reviews yourself."
    )


def _extract_workspace(ctx: Any) -> str:
    """Resolve cwd for the planner agent.

    Order:
    1. ``ctx.workspace`` — set by ``run_remedy_multi_perspective_via_executor``
       from RemedyConfig.workspace
    2. ``TARGET_REPO_PATH`` env (set by ai-dispatch.yml workflow)
    3. ``GITHUB_WORKSPACE`` env (set by all GitHub Actions runs)
    4. ``.`` fallback
    """
    if hasattr(ctx, "workspace"):
        workspace = ctx.workspace
        # RemedyConfig.workspace is a Path; codex runner wants str
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

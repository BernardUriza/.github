"""codex_resolve_generate — first half of the adversarial work pair.

Twin of ``stages/codex_review_generate.py`` and
``stages/codex_remedy_generate.py`` for the resolve pipeline.

Where review's Codex drafts findings and remedy's Codex drafts a remediation
plan, work's Codex drafts a **resolution plan** for a Linear/Plane issue: an
analysis of the issue + repo state, followed by a concrete list of files and
edits the synthesizer (Claude, with Edit/Bash/Write) should produce.

## Why two stages instead of one LLM call

Same heterogeneity argument as the review/remedy twins (plan v7 §6b). A
single-engine work agent is blind to whatever its own training distribution
misses. Codex drafts from GPT's perspective; Claude validates from
Anthropic's. Each proposed change has to survive both lenses before code is
written and committed.

## What this stage produces

Codex runs **read-only** here — no Edit/Write tools. It reads the assembled
work prompt (the issue brief + base branch context, already built by the
``build_prompt`` stage) and emits a markdown plan with sections:

    ### Analysis
    What the issue asks for, what the repo currently does, where the gap is.

    ### Proposed changes
    file:line + "what to change and why", grouped by file.

    ### Open questions
    Things Codex isn't sure about — the synthesizer should resolve via
    Read/Glob/Grep before editing.

    ### Risks
    What could break, what tests should be added/updated.

## Inputs (from prev)

- ``build_prompt.prompt``: the assembled work prompt (issue brief + base
  branch + repo context) built by ``stages/resolve_bridge.py:build_prompt``
- ``fetch_issue.identifier`` / ``title``: convenience refs for prompt framing

## Inputs (from ctx)

- ``workspace``: cwd for the agent (the cloned target repo)
- ``max_turns``: Codex tool-use turn limit (default 8 — planning needs few
  turns; the synthesizer does the heavy work)

## Output

Dict with:

- ``codex_plan`` (str): the markdown plan from Codex
- ``outcome`` (AgentRunOutcome): full runner outcome for trace
- ``succeeded`` (bool): convenience flag for downstream gating
- ``skipped`` (bool): True iff there was no prompt to act on
- ``turns``, ``tool_calls``, ``error``: telemetry for the scorecard

## Failure policy

``on_failure="continue"`` recommended at the pipeline level. If Codex
fails, the synthesizer falls back to ``claude_only`` mode — byte-equivalent
to the single-engine ``run_resolve_full_via_executor`` agent, no regression.
"""

from __future__ import annotations

import os
from typing import Any

from ..contracts.agent_runner import AgentRunner
from ..domain.agent_run import AgentRunOutcome
from ..orchestration.stage import StageFn


_CODEX_WORK_SYSTEM_PROMPT = """\
You are Codex, the GPT-5.5 planner in an adversarial resolve pipeline.

Another agent (Claude) will execute the work: write files, run commands,
commit, push, and open the draft PR. Your job is to read the issue brief +
the current state of the target repo and produce the **plan** Claude will
follow.

You are READ-ONLY. You can grep and read code but you cannot edit, run
commands, or push. Do not propose to do those things yourself — propose
them as instructions to Claude.

## Output format

Produce markdown with sections:

### Analysis
1-3 paragraphs: what does the issue ask for, what does the current code
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

### Risks
What could break with this change? What tests should be added or updated?
Cross-file ripple effects you spotted from the diff alone.

## What you do well (GPT strengths)

- Spotting the literal "implement this function" / "fix this bug" intent
  and staging the mechanical edits
- Identifying missing error paths and obvious null checks
- Spotting calls to deprecated APIs that the issue mentions

## What you might miss (and Claude will catch)

- Cross-file ripple effects of the proposed edit
- Test files that need updating alongside the source change
- Whether the change conflicts with a recent refactor in the same area
- Build/type-check failures that only surface at compile time

Empty sections are fine. Honest under-planning beats fabricated
over-planning — the synthesizer will read source anyway.
"""


def make_codex_resolve_generate_stage(
    runner: AgentRunner,
    *,
    system_prompt: str = _CODEX_WORK_SYSTEM_PROMPT,
    default_max_turns: int = 8,
) -> StageFn:
    """Factory: builds a stage function with the Codex runner closed over.

    The runner is injected so tests can substitute fakes. Production wires
    a real ``CodexCLIAgentRunner`` via the pipeline builder.

    Args:
        runner: any ``AgentRunner`` implementation
        system_prompt: work-planner system prompt; uses the default
            distilled from plan v7. Override for experiments.
        default_max_turns: turns budget when ``ctx.max_turns`` not set.
            Default 8 — planning is faster than execution; the synthesizer
            gets the higher budget (matches ``ResolveConfig.max_agent_turns``).
    """

    def codex_resolve_generate(ctx: Any, prev: dict[str, Any]) -> dict[str, Any]:
        """Invoke Codex over the work prompt, return draft resolution plan."""
        build_prompt_out = prev.get("build_prompt", {})
        user_prompt = (
            build_prompt_out.get("prompt", "")
            if isinstance(build_prompt_out, dict)
            else ""
        )
        if not user_prompt:
            # Nothing to plan against — skip gracefully so the synthesizer
            # can fall back to claude_only mode (byte-equivalent to the
            # single-engine resolve pipeline).
            return {
                "codex_plan": "",
                "outcome": AgentRunOutcome(),
                "succeeded": False,
                "skipped": True,
                "reason": "no work prompt available",
            }

        codex_prompt = _build_codex_user_prompt(user_prompt, prev)
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

    return codex_resolve_generate


# -- Helpers --------------------------------------------------------------


def _build_codex_user_prompt(work_prompt: str, prev: dict[str, Any]) -> str:
    """Wrap the assembled work prompt in planner framing.

    The work prompt itself already contains: the issue identifier, title,
    description, labels, base branch, and the instructions to the
    synthesizer. We don't re-assemble any of that — Codex reads it as-is
    and produces the plan section that Claude will follow.
    """
    fetched = prev.get("fetch_issue", {}) if isinstance(prev.get("fetch_issue"), dict) else {}
    identifier = fetched.get("identifier", "") or "(unknown issue)"
    title = fetched.get("title", "") or "(no title)"

    return (
        f"## Issue under plan: {identifier} — {title}\n"
        "\n"
        "## Original Work Brief (assembled by the build_prompt stage)\n"
        "\n"
        f"{work_prompt}\n"
        "\n"
        "---\n"
        "\n"
        "## Your Task\n"
        "\n"
        "Read the brief above, then produce your planning markdown in the "
        "format described in the system prompt. You are READ-ONLY — propose "
        "changes for Claude (the synthesizer) to execute. Do not attempt to "
        "edit files, run commands, or open PRs yourself."
    )


def _extract_workspace(ctx: Any) -> str:
    """Resolve cwd for the planner agent.

    Order:
    1. ``ctx.workspace`` — set by ``run_resolve_multi_perspective_via_executor``
       from ResolveConfig + WORKSPACE env
    2. ``WORKSPACE`` env (set by ai-resolve.yml workflow)
    3. ``GITHUB_WORKSPACE`` env (set by all GitHub Actions runs)
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
    """Resolve Codex turn budget. The synthesizer's separate budget lives
    on the same ctx field but is read by the synthesizer stage directly."""
    if hasattr(ctx, "codex_max_turns"):
        v = ctx.codex_max_turns
        if isinstance(v, int) and v > 0:
            return v
    return default

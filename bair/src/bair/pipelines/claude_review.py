"""Claude review pipeline — runs the review agent via ``claude-agent-sdk``.

Mirror of ``pipelines/remedy.py`` for the review side. Replaces the
``anthropics/claude-code-action@v1`` step inside the ``claude-review``
job of ``ai-dispatch.yml``.

## Why a separate pipeline

``remedy`` and ``review`` share the same SDK plumbing but differ in:

1. **Allowed tools.** Review is read-only — it inspects diff and posts
   findings, but never edits source. So no ``Edit``/``Write``. ``Bash``
   is narrowed to ``gh api`` / ``gh pr`` calls for posting reviews and
   inline comments (the SDK has no built-in MCP equivalent of
   ``mcp__github_inline_comment__create_inline_comment``).
2. **No "approve mode" concept.** Review's terminal action is always to
   submit a review event (APPROVE / COMMENT / REQUEST_CHANGES) per the
   ``claude-review.md`` prompt contract — there is no parallel
   "approve-only" path the way ``/ai-remedy approve`` has.
3. **Turn budget.** Review's prompt is shorter and read-only; default
   max turns is correspondingly lower (40 vs remedy's 100). Override
   via ``REVIEW_MAX_TURNS``.

## Why no MCP

The ``mcp__github_inline_comment__create_inline_comment`` tool the
prior action invocation used is bundled with
``anthropics/claude-code-action`` and is not exposed by
``ClaudeSDKAgentRunner``. The replacement is a direct
``gh api repos/<owner>/<repo>/pulls/<N>/comments`` POST — same GitHub
REST endpoint, no MCP server required. The ``claude-review.md`` prompt
template instructs the agent in that idiom.

## Invocation contract

Called from ``bair.__main__`` as the ``claude-review`` Typer command.
Reads config from env vars set by ``ai-dispatch.yml`` (claude-review job):

- ``REPO``: ``owner/name``
- ``PR_NUM``: PR number (string, converted to int)
- ``PROMPT_FILE``: path to the prompt artifact, default ``/tmp/claude-prompt.md``
- ``TARGET_REPO_PATH``: cwd where the agent runs (the cloned PR repo)
- ``REVIEW_MAX_TURNS``: optional turn budget override, default 40
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..domain.agent_run import AgentRunOutcome
from ..infra.agent_runner import ClaudeSDKAgentRunner
from ..log import logger


@dataclass
class ClaudeReviewConfig:
    """Config for a Claude review run. Populated from env in ``from_env``."""

    repo: str
    pr_number: int
    prompt_file: Path
    workspace: Path
    max_turns: int

    @classmethod
    def from_env(cls) -> "ClaudeReviewConfig":
        repo = os.environ.get("REPO", "")
        if not repo:
            raise SystemExit("REPO env var is required (format: owner/name)")
        pr_num_raw = os.environ.get("PR_NUM", "")
        if not pr_num_raw:
            raise SystemExit("PR_NUM env var is required")
        try:
            pr_number = int(pr_num_raw)
        except ValueError:
            raise SystemExit(f"PR_NUM must be integer, got: {pr_num_raw!r}")
        prompt_file = Path(os.environ.get("PROMPT_FILE", "/tmp/claude-prompt.md"))
        workspace = Path(
            os.environ.get(
                "TARGET_REPO_PATH",
                os.environ.get("GITHUB_WORKSPACE", "."),
            )
        )
        max_turns = int(os.environ.get("REVIEW_MAX_TURNS", "40"))
        return cls(
            repo=repo,
            pr_number=pr_number,
            prompt_file=prompt_file,
            workspace=workspace,
            max_turns=max_turns,
        )


def run_claude_review(cfg: ClaudeReviewConfig) -> None:
    """Execute the Claude review pipeline. Raises SystemExit on agent failure."""
    t_start = time.monotonic()

    logger.info("=" * 60)
    logger.info(f"CLAUDE REVIEW PIPELINE — PR #{cfg.pr_number} on {cfg.repo}")
    logger.info(f"  Workspace:    {cfg.workspace}")
    logger.info(f"  Max turns:    {cfg.max_turns}")
    logger.info("=" * 60)

    # [1/3] Load prompt artifact written by dispatch.py
    if not cfg.prompt_file.exists():
        raise SystemExit(
            f"Prompt artifact not found: {cfg.prompt_file}. "
            f"The dispatch job should have written it before this step runs."
        )
    user_prompt = cfg.prompt_file.read_text(encoding="utf-8")
    logger.info(f"[1/3] Loaded prompt — {len(user_prompt)} chars")

    # [2/3] Run agent. Read-only tool set — no Edit/Write. Bash is allowed
    # generally so the agent can call `gh api` for posting inline comments
    # and submitting the review event. The prompt narrows the agent to
    # gh / curl / jq idioms; broader Bash latitude here mirrors how the
    # action invocation's allowedTools were specified.
    logger.info(f"[2/3] Running Claude SDK")

    runner = ClaudeSDKAgentRunner(
        model="claude-sonnet-4-6",
        allowed_tools=(
            "Read",
            "Bash",
            "Glob",
            "Grep",
        ),
        permission_mode="bypassPermissions",
    )

    t_agent = time.monotonic()
    # The review prompt is self-contained — no separate system prompt needed.
    # The prompt builder (claude_builder.py) puts all role instructions,
    # findings rubric, and posting contract inside the user message.
    outcome: AgentRunOutcome = runner.run(
        user_prompt=user_prompt,
        system_prompt="",
        cwd=str(cfg.workspace),
        max_turns=cfg.max_turns,
    )
    elapsed_agent = time.monotonic() - t_agent

    logger.info(
        f"  Agent finished: {outcome.turns} turns, "
        f"{outcome.tool_calls} tool calls "
        f"({outcome.edit_calls} Edit/Write), "
        f"result_text={len(outcome.result_text)} chars, "
        f"elapsed={elapsed_agent:.1f}s"
    )
    if outcome.edit_calls:
        # Review is supposed to be read-only. If the agent edited anything,
        # that's a contract violation — flag it loudly. Not fatal: surface
        # to the Job Summary and let the human auditor decide.
        logger.warning(
            f"  Agent ran {outcome.edit_calls} Edit/Write call(s) — "
            f"review is supposed to be read-only"
        )
    if outcome.error:
        logger.error(f"  Agent error: {outcome.error}")

    # [3/3] Job summary
    logger.info("[3/3] Writing Job Summary")
    elapsed_total = time.monotonic() - t_start
    _write_summary(cfg, outcome, elapsed_agent, elapsed_total)

    logger.info("=" * 60)
    logger.info(f"CLAUDE REVIEW COMPLETE — {elapsed_total:.1f}s total")
    logger.info("=" * 60)

    # Re-raise as SystemExit so the GitHub Actions step fails non-zero
    # when the agent crashed. Mirrors remedy.py's behavior — without this
    # the workflow proceeds as if review succeeded when the agent never
    # actually posted anything.
    if outcome.error:
        raise SystemExit(f"Claude review agent failed: {outcome.error}")


def _write_summary(
    cfg: ClaudeReviewConfig,
    outcome: AgentRunOutcome,
    elapsed_agent: float,
    elapsed_total: float,
) -> None:
    """Append a rich ludic markdown summary to $GITHUB_STEP_SUMMARY.

    Delegates to ``prompt.agent_summary.render_agent_summary`` — same
    shared renderer remedy uses.
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    from ..prompt.agent_summary import render_agent_summary

    extra_lines = (
        f"**Wall-clock:** agent {elapsed_agent:.1f}s · total {elapsed_total:.1f}s",
    )

    markdown = render_agent_summary(
        outcome,
        kind="review (Python SDK)",
        repo=cfg.repo,
        pr_number=cfg.pr_number,
        extra_header_lines=extra_lines,
    )

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(markdown)

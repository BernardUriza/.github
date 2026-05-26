"""Remedy pipeline — autonomous PR remediation via Claude Agent SDK.

Replaces the prior ``anthropics/claude-code-action@v1`` invocation in the
``claude-remedy`` job of ``ai-dispatch.yml``. Same agent, same prompt, same
OAuth/Max billing path — but invoked through Python so we control output
formatting, expose proper per-tool telemetry, and stop dumping raw
stream-json to the step log.

## Why this exists

The previous `claude-code-action@v1` invocation dumped 14000-line raw JSONL
to the step log when ``show_full_output: true``. The post-hoc rich summary
step we added in commit dba6c3b parses execution_file retrospectively, but
the live step log stayed unreadable. This pipeline runs the agent
in-process via ``ClaudeSDKAgentRunner``, which logs each tool call as a
plain Python line — instantly readable.

## What it does

1. Loads the remedy prompt from the artifact written by ``dispatch.py``
   (``write_claude_remedy_prompt_file``). The prompt already instructs the
   agent to commit, push, and submit the final review event via
   ``gh api .../reviews`` — this pipeline doesn't post the review itself.
2. Instantiates ``ClaudeSDKAgentRunner`` with the write-capable toolset
   (Read/Edit/Bash/Glob/Grep/Write). No MCP server config needed — the
   prompt uses ``gh api`` for everything, including inline comments if
   the agent decides to post them.
3. Runs the agent. ``ClaudeSDKAgentRunner`` already removes
   ``ANTHROPIC_API_KEY`` when ``CLAUDE_CODE_OAUTH_TOKEN`` is set, falling
   through to OAuth/Max billing — same path the action used after the
   M6.2 OAuth migration.
4. Emits a rich Job Summary with per-tool counts, duration, turns,
   destructive command flags, and the agent's final message text. No
   execution_file parsing — the runner returns ``AgentRunOutcome``
   directly with the same data.

## Tools NOT enabled

- ``mcp__github_inline_comment__create_inline_comment``: requires MCP
  server config that ``ClaudeSDKAgentRunner`` doesn't currently support.
  The agent can still post inline comments via
  ``Bash(gh api repos/.../pulls/<N>/comments)`` — same endpoint, no MCP
  required.

## Invocation contract

Called from ``bair.__main__`` as the ``remedy`` Typer command.
Reads config from env vars set by ``ai-dispatch.yml``:

- ``REPO``: ``owner/name``
- ``PR_NUM``: PR number (string, converted to int)
- ``PROMPT_FILE``: path to the prompt artifact, default ``/tmp/claude-prompt.md``
- ``TARGET_REPO_PATH``: cwd where the agent runs (the cloned PR repo)
- ``REMEDY_MAX_TURNS``: optional turn budget override, default 60
- ``APPROVE_MODE``: ``"true"``/``"false"`` for telemetry only (the prompt
  itself already routes APPROVE vs COMMENT logic)
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
class RemedyConfig:
    """Config for a remedy run. Populated from env in ``from_env``."""

    repo: str
    pr_number: int
    prompt_file: Path
    workspace: Path
    max_turns: int
    approve_mode: bool

    @classmethod
    def from_env(cls) -> "RemedyConfig":
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
        max_turns = int(os.environ.get("REMEDY_MAX_TURNS", "100"))
        approve_mode = os.environ.get("APPROVE_MODE", "").lower() in (
            "1",
            "true",
            "yes",
        )
        return cls(
            repo=repo,
            pr_number=pr_number,
            prompt_file=prompt_file,
            workspace=workspace,
            max_turns=max_turns,
            approve_mode=approve_mode,
        )


def run_remedy(cfg: RemedyConfig) -> None:
    """Execute the remedy pipeline. Raises SystemExit on agent failure."""
    t_start = time.monotonic()

    logger.info("=" * 60)
    logger.info(f"REMEDY PIPELINE — PR #{cfg.pr_number} on {cfg.repo}")
    logger.info(f"  Approve mode: {cfg.approve_mode}")
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

    # [2/3] Run agent
    logger.info(f"[2/3] Running Claude SDK")

    runner = ClaudeSDKAgentRunner(
        model="claude-sonnet-4-6",
        allowed_tools=(
            "Read",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
            "Write",
        ),
        permission_mode="bypassPermissions",
    )

    t_agent = time.monotonic()
    # The remedy prompt is self-contained — no separate system prompt needed.
    # The prompt builder (claude_remedy_builder.py) puts all role instructions,
    # APPROVE/COMMENT contract, and the merge-gate rubric inside the user
    # message, mirroring how the action was invoked.
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
    if outcome.destructive_calls:
        logger.warning(
            f"  Agent ran {len(outcome.destructive_calls)} destructive command(s) "
            f"that mutate node_modules / deps"
        )
        for cmd in outcome.destructive_calls:
            logger.warning(f"    - {cmd[:200]}")
    if outcome.error:
        logger.error(f"  Agent error: {outcome.error}")

    # [3/3] Job summary
    logger.info("[3/3] Writing Job Summary")
    elapsed_total = time.monotonic() - t_start
    _write_summary(cfg, outcome, elapsed_agent, elapsed_total)

    logger.info("=" * 60)
    logger.info(f"REMEDY COMPLETE — {elapsed_total:.1f}s total")
    logger.info("=" * 60)

    # Re-raise as SystemExit so the GitHub Actions step fails non-zero
    # when the agent crashed. Without this the workflow proceeds as if
    # the remedy succeeded — exactly the failure mode that gave us the
    # 401 / "agent did not post review body" confusion on PR #671.
    if outcome.error:
        raise SystemExit(f"Remedy agent failed: {outcome.error}")


def _write_summary(
    cfg: RemedyConfig,
    outcome: AgentRunOutcome,
    elapsed_agent: float,
    elapsed_total: float,
) -> None:
    """Append a rich ludic markdown summary to $GITHUB_STEP_SUMMARY.

    Delegates to ``prompt.agent_summary.render_agent_summary`` — the shared
    renderer used (or to-be-used) across remedy/review/resolve pipelines so
    every Job Summary looks the same.

    Best-effort: silently no-ops when running locally (no $GITHUB_STEP_SUMMARY).
    """
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    from ..prompt.agent_summary import render_agent_summary

    extra_lines = (
        f"**Approve mode:** `{cfg.approve_mode}`",
        f"**Wall-clock:** agent {elapsed_agent:.1f}s · total {elapsed_total:.1f}s",
    )

    markdown = render_agent_summary(
        outcome,
        kind="remedy (Python SDK)",
        repo=cfg.repo,
        pr_number=cfg.pr_number,
        extra_header_lines=extra_lines,
    )

    with open(summary_path, "a", encoding="utf-8") as f:
        f.write(markdown)

"""Issue Rank pipeline — fetch, assess, rank, publish.

fetch(tracker) → assess(GPT readiness) → score → rank → output(JSON + summary)
"""

from __future__ import annotations

import json
import time

from ..config.issue_rank import IssueRankConfig
from ..domain.issue_scoring import rank_issues
from ..gatherers.issues import gather_issues
from ..infra.container import Container
from ..log import logger
from .. import __version__


def run_issue_rank(container: Container, cfg: IssueRankConfig | None = None) -> None:
    """Execute the full issue ranking pipeline."""
    c = container
    if cfg is None:
        cfg = IssueRankConfig.from_env()

    t_start = time.monotonic()
    logger.info("=" * 60)
    logger.info(f"BAIR v{__version__} | issue-rank for team {cfg.team_key}")
    logger.info("=" * 60)

    # ── Step 1: Fetch issues from the tracker ────────────────────
    logger.info("[1/3] FETCH from tracker")
    if c.tracker is None:
        c.actions.error("IssueTrackerClient not available — set PLANE_API_KEY")
        return

    issues = c.tracker.list_issues(cfg.team_key)
    logger.info(f"  {len(issues)} open issues fetched")

    if not issues:
        logger.info("  No open issues. Nothing to rank.")
        _write_output([], c, cfg)
        return

    # ── Step 2: Assess readiness via GPT ─────────────────────────
    logger.info("[2/3] ASSESS readiness (GPT)")
    scored = gather_issues(issues, c.llm)
    logger.info(f"  {len(scored)} issues scored")

    # ── Step 3: Rank ─────────────────────────────────────────────
    logger.info("[3/3] RANK")
    ranked = rank_issues(scored)
    top = ranked[:cfg.top_n]

    # ── Output ───────────────────────────────────────────────────
    _write_output(top, c, cfg)

    total_time = time.monotonic() - t_start
    logger.info("=" * 60)
    logger.info(f"BAIR v{__version__} | ISSUE RANK COMPLETE")
    logger.info(f"  Team:      {cfg.team_key}")
    logger.info(f"  Assessed:  {len(scored)} issues")
    logger.info(f"  Top {cfg.top_n}:    {', '.join(s.identifier for s in top)}")
    logger.info(f"  Time:      {total_time:.1f}s")
    logger.info("=" * 60)


def _write_output(
    ranked: list,
    c: Container,
    cfg: IssueRankConfig,
) -> None:
    """Write ranked issues to JSON artifact + Actions summary."""
    output = []
    for s in ranked:
        output.append({
            "rank": s.rank,
            "identifier": s.identifier,
            "title": s.title,
            "project": s.project_name,
            "assignee": s.assignee_name,
            "total_score": round(s.total_score, 2),
            "scores": {
                "priority": round(s.score_priority, 2),
                "project": round(s.score_project, 2),
                "readiness": round(s.score_readiness, 2),
                "feasibility": round(s.score_feasibility, 2),
                "staleness": round(s.score_staleness, 2),
            },
            "readiness_reason": s.readiness_reason,
        })

    # Write JSON artifact
    c.store.write("issue-rank.json", json.dumps(output, indent=2))

    if cfg.dry_run:
        logger.info("--- DRY RUN: Issue Ranking ---")
        for item in output:
            logger.info(
                f"  #{item['rank']} {item['identifier']} "
                f"({item['total_score']}) — {item['title'][:60]}"
            )
        return

    # Write Actions summary
    lines = ["# BAIR Issue Ranking\n"]
    lines.append("| Rank | Issue | Score | Project | Readiness |")
    lines.append("|------|-------|-------|---------|-----------|")
    for item in output:
        lines.append(
            f"| {item['rank']} | **{item['identifier']}** {item['title'][:40]} "
            f"| {item['total_score']} "
            f"| {item['project'] or '—'} "
            f"| {item['readiness_reason'][:50]} |"
        )
    lines.append(f"\n*{len(output)} issues ranked by BAIR v{__version__}*")
    c.actions.write_summary("\n".join(lines))

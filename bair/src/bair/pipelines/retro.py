"""AI Retro pipeline — post-review retrospective analysis.

Gathers evidence from a PR, sends to LLM for analysis, writes Job Summary.
Does NOT modify any files, learnings, or repo state.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config.retro import RetroConfig
from ..domain.models import PRIdentifier
from ..infra.constants import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE
from ..infra.container import Container
from ..gatherers.retro_evidence import gather_retro_evidence
from ..log import logger


def _render_summary(pr: PRIdentifier, evidence: object, result: dict, variant: str, guidance: str = "") -> str:
    """Render the full Job Summary markdown."""
    ev = evidence  # type: ignore
    parts: list[str] = []

    # Header
    parts.append(f"# AI Retro — PR #{pr.number}")
    parts.append("")
    parts.append(f"## Summary")
    parts.append(result.get("summary", "No summary produced."))
    parts.append("")

    # Operator guidance (if provided)
    if guidance:
        parts.append("## Operator Guidance")
        parts.append("")
        parts.append(f"> {guidance}")
        parts.append("")
        parts.append("*This guidance was provided as advisory input. Claims were validated against PR evidence.*")
        parts.append("")

    # Evidence section (collapsible)
    parts.append("<details><summary>Evidence Reviewed</summary>")
    parts.append("")
    parts.append(f"| Field | Value |")
    parts.append(f"|-------|-------|")
    parts.append(f"| PR | {pr.repo}#{pr.number} |")
    parts.append(f"| State | {ev.pr_state} |")
    parts.append(f"| Title | {ev.pr_title} |")
    parts.append(f"| Size | +{ev.pr_additions}/-{ev.pr_deletions}, {ev.pr_changed_files} files |")
    if ev.pr_merged_by:
        parts.append(f"| Merged by | {ev.pr_merged_by} |")
    parts.append(f"| AI Reviews | {len(ev.ai_reviews)} |")
    parts.append(f"| Human Comments | {len(ev.human_comments)} |")
    parts.append(f"| Threads | {ev.resolved_threads}/{ev.thread_count} resolved |")
    parts.append(f"| Variant | {variant} |")
    parts.append("")
    parts.append("</details>")
    parts.append("")

    # AI review body (collapsible)
    if ev.ai_reviews:
        parts.append("<details><summary>AI Review(s) Posted</summary>")
        parts.append("")
        for rev in ev.ai_reviews:
            parts.append(f"**Review** ({rev.state}, {rev.submitted_at})")
            parts.append("")
            parts.append(rev.body)
            parts.append("")
        parts.append("</details>")
        parts.append("")

    # Finding outcomes table
    outcomes = result.get("finding_outcomes", [])
    if outcomes:
        parts.append("## Finding Outcomes")
        parts.append("")
        parts.append("| # | Finding | Outcome | Evidence |")
        parts.append("|---|---------|---------|----------|")
        icons = {"true_positive": "✅", "false_positive": "❌", "ignored": "⏭️", "fragile": "⚠️"}
        for i, f in enumerate(outcomes, 1):
            icon = icons.get(f.get("outcome", ""), "❓")
            summary = f.get("finding_summary", "")[:80]
            outcome = f.get("outcome", "unknown")
            evidence = f.get("evidence", "")[:100]
            parts.append(f"| {i} | {summary} | {icon} {outcome} | {evidence} |")
        parts.append("")
    else:
        parts.append("## Finding Outcomes")
        parts.append("No AI findings to evaluate.")
        parts.append("")

    # Missed issues
    missed = result.get("missed_issues", [])
    if missed:
        parts.append("## Missed Issues")
        parts.append("")
        for m in missed:
            sev = m.get("severity", "info")
            parts.append(f"**{sev}** — `{m.get('file', '?')}` — {m.get('description', '')}")
            parts.append(f"> Why missed: {m.get('why_missed', 'Unknown')}")
            parts.append("")

    # Candidate learnings (main output)
    candidates = result.get("candidate_learnings", [])
    if candidates:
        parts.append("## Candidate Learnings")
        parts.append("")
        parts.append("These are **proposals for human curation**. Nothing has been written to the learnings database.")
        parts.append("")
        for i, c in enumerate(candidates, 1):
            parts.append(f"### {i}. [{c.get('type', '?')}] {c.get('rule', 'No rule')}")
            parts.append(f"- **Confidence**: {c.get('confidence', '?')}")
            parts.append(f"- **Boundary**: {c.get('decision_boundary', '?')}")
            parts.append(f"- **Evidence**: {c.get('source_evidence', '?')}")
            parts.append("")
            parts.append(f"<details><summary>Suggested YAML</summary>")
            parts.append("")
            parts.append("```yaml")
            parts.append(f"- type: {c.get('type', '?')}")
            parts.append(f'  rule: "{c.get("rule", "")}"')
            parts.append(f'  decision_boundary: "{c.get("decision_boundary", "")}"')
            parts.append(f"  confidence: {c.get('confidence', 'low')}")
            parts.append("```")
            parts.append("")
            parts.append("</details>")
            parts.append("")
    else:
        parts.append("## Candidate Learnings")
        parts.append("No reusable learnings identified in this PR.")
        parts.append("")

    return "\n".join(parts)


def run_retro(container: Container, cfg: RetroConfig | None = None) -> None:
    """Execute the retrospective analysis pipeline."""
    c = container
    if cfg is None:
        cfg = RetroConfig.from_env()

    pr = PRIdentifier.from_env(cfg.repo, cfg.pr_num)
    logger.info(f"AI Retro: analyzing {pr.repo}#{pr.number}")

    # ── Gather evidence (no LLM) ────────────────────────────────
    evidence = gather_retro_evidence(pr, c.github)
    logger.info(f"Evidence: {len(evidence.ai_reviews)} reviews, {len(evidence.human_comments)} comments")

    if not evidence.ai_reviews:
        c.actions.notice("No AI reviews found on this PR — retro will have limited analysis")

    # ── LLM synthesis ────────────────────────────────────────────
    prompt_path = Path(f"prompts/retro.md")
    system_prompt = prompt_path.read_text(encoding="utf-8")

    user_message = evidence.to_llm_context()

    if cfg.guidance:
        user_message += f"\n\n## Operator Guidance (advisory — validate against evidence)\n{cfg.guidance}"
        logger.info(f"Operator guidance injected: {cfg.guidance[:200]}")

    if evidence.diff:
        user_message += f"\n\n## PR Diff (first 60K chars)\n```\n{evidence.diff[:60_000]}\n```"

    logger.info(f"Calling LLM for retro analysis...")
    raw = c.llm.call(
        system=system_prompt,
        user=user_message,
        model=cfg.model,
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=DEFAULT_TEMPERATURE,
    )

    # Parse result
    if "_raw" in raw:
        logger.warning(f"::warning::LLM returned non-JSON response")
        raw = {"summary": "Analysis produced non-JSON output.", "finding_outcomes": [], "missed_issues": [], "candidate_learnings": []}

    # ── Render Job Summary ───────────────────────────────────────
    summary_md = _render_summary(pr, evidence, raw, cfg.variant, cfg.guidance)
    c.actions.write_summary(summary_md)
    logger.info(f"Retro written to Job Summary ({len(summary_md)} chars)")

    # Also print to stdout for local testing (safe for Windows cp1252)
    try:
        logger.info("\n" + summary_md)
    except UnicodeEncodeError:
        logger.info("\n" + summary_md.encode("ascii", errors="replace").decode("ascii"))

"""Review pipeline -- the single orchestrator for the full review flow.

gather -> prompt -> llm -> validate -> guardrail -> publish -> trace

Changes from v1:
- Deep analysis uses container.get_deep_llm() (protocol-based, retry for free)
- Publish uses ReviewPublisher strategy (replaces dry_run boolean branching)
- Hallucination filter extracted to domain (pure, testable)

DEPRECATED — to be removed when VAIR_REVIEW_MULTI_PERSPECTIVE flag is
removed from dispatch.py.

This monolith has no production callers as of 2026-05-11. Both
``__main__.review`` and ``dispatch._run_review_gpt`` route exclusively
through ``pipelines/review_via_executor.py``'s declarative pipeline. The
``services/local_runner.py:run_local_review`` consumer was migrated to
``run_review_full_via_executor`` in the same audit cleanup.

The only remaining external reference is
``stages/review_bridge.py::generate_review_stage`` importing
``resolve_deep_analysis`` from this module — a future cleanup should move
that helper to ``domain/`` or ``pipelines/_helpers/`` and finish the
deletion.

Audit reference:
https://github.com/bernard-org/engineering-notes/blob/main/audits/bair-multi-perspective-2026-05-11/README.md#finding-1
"""

from __future__ import annotations

import json
from dataclasses import replace

from ..config import ReviewConfig
from ..config.review import DeepMode
from ..domain.deep_heuristic import should_auto_deep
from ..domain.exceptions import ValidationError
from ..domain.models import PRIdentifier, ReviewContext, ReviewResponse
from ..domain.publishing import (
    DryRunPublisher,
    GitHubPublisher,
    ReviewPublisher,
    check_existing_review,
)
from ..infra.constants import REVIEW_JSON
from ..infra.container import Container
from ..log import logger
from ..gatherers.ci_status import gather_ci_status
from ..gatherers.css_health import gather_css_health
from ..gatherers.diff import gather_diff
from ..gatherers.file_context import gather_file_context
from ..gatherers.learnings import gather_learnings
from ..gatherers.prior_art import gather_prior_art
from ..gatherers.rules import condense_rules, gather_rules
from ..gatherers.threads import gather_threads
from ..prompt.builder import build_prompt
from ..prompt.trace_formatter import write_trace


# -- Guardrails --------------------------------------------------------

def _validate_findings_against_diff(
    review: ReviewResponse, diff_text: str,
) -> tuple[ReviewResponse, int]:
    """Drop findings that reference files not present in the diff.

    Returns (filtered_review, dropped_count) so the publisher knows
    whether zero findings means "clean PR" or "all findings hallucinated".
    """
    diff_files: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                diff_files.add(parts[1])
        elif line.startswith("+++ b/"):
            diff_files.add(line[6:])

    if not diff_files:
        return review, 0

    valid_findings = []
    dropped = 0
    for f in review.findings:
        if f.file and f.file not in diff_files:
            logger.warning(f"::warning::Dropped hallucinated finding for {f.file} (not in diff)")
            dropped += 1
            continue
        valid_findings.append(f)

    if dropped > 0:
        return ReviewResponse(
            title=review.title,
            summary=review.summary,
            findings=valid_findings,
            highlights=review.highlights,
            knowledge=review.knowledge,
        ), dropped
    return review, 0


# -- Deep analysis decision --------------------------------------------

def resolve_deep_analysis(
    cfg: ReviewConfig, diff: str, container: Container,
) -> str:
    """Decide whether to run deep analysis and execute if needed.

    Consolidates the DeepMode enum, auto-heuristic, and gatherer call
    into one function with clear logging.
    """
    if cfg.deep_mode == DeepMode.DISABLED:
        logger.info("Deep mode: DISABLED by user")
        return ""

    run_deep = False
    if cfg.deep_mode == DeepMode.FORCED:
        run_deep = True
        logger.info("Deep mode: FORCED by user")
    else:
        changed_file_count = sum(
            1 for line in diff.splitlines() if line.startswith("+++ b/")
        )
        run_deep, reasons = should_auto_deep(diff, changed_file_count)
        reason_str = ", ".join(reasons)
        direction = "ON" if run_deep else "OFF"
        logger.info(f"Deep mode: AUTO -> {direction} ({reason_str})")

    if not run_deep:
        return ""

    from ..gatherers.deep_analysis import gather_deep_analysis

    try:
        return gather_deep_analysis(
            pr=PRIdentifier.from_env(cfg.repo, cfg.pr_num),
            diff=diff,
            deep_llm=container.get_deep_llm(),
            store=container.store,
            actions=container.actions,
        )
    except Exception as e:
        container.actions.warning(f"Deep analysis failed \u2014 continuing GPT-only: {e}")
        return ""


# -- Publisher factory --------------------------------------------------

def _make_publisher(cfg: ReviewConfig, container: Container) -> ReviewPublisher:
    """Select the publish strategy based on config."""
    if cfg.dry_run:
        return DryRunPublisher()
    return GitHubPublisher(github=container.github, actions=container.actions)


# -- Pipeline entry point ----------------------------------------------

def run_review(container: Container, cfg: ReviewConfig | None = None) -> None:
    """Execute the full review pipeline."""
    c = container
    if cfg is None:
        cfg = ReviewConfig.from_env()

    pr = PRIdentifier.from_env(cfg.repo, cfg.pr_num)

    # -- Dedup check FIRST — before spending any tokens ----------------
    if not cfg.dry_run and not cfg.skip_dedup and check_existing_review(c.github, pr):
        c.actions.notice("Review already exists for latest commit — skipping")
        # Tell the user WHY nothing happened
        try:
            c.github.run_gh(
                "api", f"repos/{pr.repo}/issues/{pr.number}/comments",
                "--method", "POST", "-f",
                f"body=Review already exists for this commit — skipping duplicate. To force a new review, push a new commit.",
                check=False,
            )
        except Exception:
            pass
        logger.info("Dedup: review exists for latest commit. $0 spent. Exiting.")
        return

    ctx = ReviewContext(
        pr=pr, model=cfg.model,
        max_diff_bytes=cfg.max_diff_bytes,
        prompt_file=cfg.prompt_file,
    )

    # -- Gather context ------------------------------------------------
    import time as _time
    from .. import __version__
    _t_start = _time.monotonic()

    logger.info("=" * 60)
    logger.info(f"BAIR v{__version__} | reviewing PR #{cfg.pr_num} on {cfg.repo}")
    logger.info("=" * 60)
    logger.info("[1/7] DIFF")
    diff, truncated = gather_diff(pr, cfg.max_diff_bytes, c.github, c.store, c.actions)
    ctx = replace(ctx, diff=diff, truncated=truncated)
    diff_lines = diff.count("\n")
    logger.info(f"  {len(diff)} bytes, {diff_lines} lines, truncated={truncated}")

    logger.info("=" * 60)
    logger.info("[2/7] RULES (raw)")
    learnings = gather_learnings(cfg.variant, c.store, c.actions) if cfg.variant else ""
    rules, selected_rules = gather_rules(pr, c.github, c.store, c.actions)
    logger.info(f"  raw size: {len(rules)} bytes")

    logger.info("=" * 60)
    logger.info("[3/7] RULES CONDENSATION (LLM call)")
    t0 = _time.monotonic()
    rules = condense_rules(rules, c.llm, c.actions)
    logger.info(f"  condensed size: {len(rules)} bytes ({_time.monotonic() - t0:.1f}s)")

    logger.info("=" * 60)
    logger.info("[4/7] CONTEXT (threads, prior art, CSS, files)")
    resolved_threads = gather_threads(pr, c.github, c.store, c.actions)
    prior_art = gather_prior_art(pr.repo, diff, c.github, c.store, c.actions)
    css_health = gather_css_health(pr, c.github, c.store, c.actions)
    ci_status = gather_ci_status(pr, c.github, c.store, c.actions)
    full_files = gather_file_context(pr, c.github, c.store, c.actions)

    logger.info("=" * 60)
    logger.info("[5/7] DEEP ANALYSIS (Agent SDK — Claude with repo tools)")
    t0 = _time.monotonic()
    deep_analysis = resolve_deep_analysis(cfg, diff, c)
    da_time = _time.monotonic() - t0
    logger.debug(f"  [review] _resolve_deep_analysis returned type={type(deep_analysis).__name__}, len={len(deep_analysis)}")
    if deep_analysis:
        logger.debug(f"  [review] result: {len(deep_analysis)} chars ({da_time:.1f}s)")
        logger.debug(f"  [review] preview: {deep_analysis[:300]}")
        logger.debug(f"  [review] starts_with_digit: {deep_analysis.strip()[:1].isdigit()}")
    else:
        logger.debug(f"  [review] result: EMPTY ({da_time:.1f}s)")

    ctx = replace(
        ctx,
        learnings=learnings, rules=rules, selected_rules=selected_rules,
        resolved_threads=resolved_threads, prior_art=prior_art,
        css_health=css_health, ci_status=ci_status,
        deep_analysis=deep_analysis, full_files=full_files,
    )
    logger.debug(f"  [review] ctx.deep_analysis after replace: len={len(ctx.deep_analysis)}")

    # -- Build prompt + call LLM ---------------------------------------
    logger.info("=" * 60)
    logger.info("[6/7] GPT REVIEW (primary reviewer)")
    logger.debug(f"  [review] PRE build_prompt: ctx.deep_analysis len={len(ctx.deep_analysis)}")
    ctx = build_prompt(ctx)
    logger.debug(f"  [review] POST build_prompt: user_message len={len(ctx.user_message)}")
    logger.debug(f"  [review] '[DEEP ANALYSIS' in user_message: {'[DEEP ANALYSIS' in ctx.user_message}")
    logger.debug(f"  [review] 'data_flow' in user_message: {'data_flow' in ctx.user_message}")
    t0 = _time.monotonic()
    raw = c.llm.call(
        system=ctx.system_prompt,
        user=ctx.user_message,
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
    )
    logger.debug(f"  done in {_time.monotonic() - t0:.1f}s")
    logger.debug(f"  total pipeline: {_time.monotonic() - _t_start:.1f}s")

    # -- Validate + guardrail ------------------------------------------
    try:
        review = ReviewResponse.validate(raw)
    except ValidationError as e:
        c.actions.warning(f"LLM output validation failed: {e}")
        review = ReviewResponse.empty()

    review, dropped_count = _validate_findings_against_diff(review, diff)
    ctx = replace(ctx, review=review)

    c.store.write(REVIEW_JSON, json.dumps(raw, indent=2))
    logger.debug("--- Review output ---")
    logger.debug(json.dumps(raw, indent=2)[:2000])

    # -- Narrativize body -----------------------------------------------
    from ..domain.narrator import narrativize
    t0 = _time.monotonic()
    narrative = narrativize(review, c.llm)
    logger.info(f"  Narrator: {_time.monotonic() - t0:.1f}s")

    # -- Publish -------------------------------------------------------
    logger.info("[7/7] Publishing findings...")
    publisher = _make_publisher(cfg, c)
    publisher.publish(
        review, pr, cfg.run_id, truncated, cfg.max_diff_bytes,
        dropped_count, narrative=narrative, diff_text=diff,
    )

    # -- Trace (always) ------------------------------------------------
    write_trace(ctx, c.actions, cfg.run_id)

    # -- Final summary (always last) ------------------------------------
    total_time = _time.monotonic() - _t_start
    n_findings = len(review.findings) if hasattr(review, "findings") else 0
    deep_tag = f"{len(deep_analysis)} chars" if deep_analysis else "OFF"

    logger.info("=" * 60)
    logger.info(f"BAIR v{__version__} | REVIEW COMPLETE")
    logger.info(f"  PR:             #{cfg.pr_num} on {cfg.repo}")
    logger.info(f"  Diff:           {len(diff)} bytes, {diff.count(chr(10))} lines")
    logger.info(f"  Rules:          {len(rules)} bytes")
    logger.info(f"  Deep analysis:  {deep_tag}")
    logger.info(f"  Findings:       {n_findings}")
    logger.info(f"  Model:          {cfg.model}")
    logger.info(f"  Total time:     {total_time:.1f}s")
    logger.info("=" * 60)

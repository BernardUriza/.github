"""assemble_review_from_synthesis — bridge between the adversarial pair and
the existing narrativize/publish stages.

Takes the ``synthesized_payload`` dict produced by
``claude_review_synthesize`` and converts it into the SAME output shape that
``generate_review_stage`` produces:

    {
        "review": ReviewResponse,          # validated + hallucinations dropped
        "raw": dict,                       # the original synthesizer payload
        "dropped_count": int,
        "review_ctx": ReviewContext,       # for narrativize + write_trace
    }

This means the rest of the multi-perspective pipeline (narrativize, publish,
write_trace) is unchanged — it sees a ``generate_review`` output exactly the
way the single-engine pipeline produces one.

## DEFERRED REFACTOR (audit Finding 2)

This stage is **a transition artifact, not a permanent design.** When
multi-perspective output diverges from ``ReviewResponse`` — needs to carry
per-perspective confidence, disagreement deltas, Codex-vs-Claude diffs, or
any field that doesn't fit the single-engine shape — refactor
``narrativize_stage`` and ``publish_review_stage`` to accept the richer
shape, and **delete this stage entirely.**

Trigger conditions for the refactor (whichever fires first):

1. A new field needs to flow from ``synthesize`` to ``publish`` that
   ``ReviewResponse`` can't carry without dead fields on the single-engine
   path. The forced-issue trigger.
2. ``VAIR_REVIEW_MULTI_PERSPECTIVE`` flag is removed (audit Finding 3 —
   7-day green staging trigger). At that point single-engine is dead and
   the dual shape is no longer load-bearing.

Tracking issue: https://github.com/bernard-org/.github/issues/68
Audit: https://github.com/bernard-org/engineering-notes/blob/main/audits/bair-multi-perspective-2026-05-11/README.md#finding-2--assemble_review_from_synthesis-is-a-compatibility-shim

## Why a separate stage and not inline in claude_review_synthesize

Separation of concerns:

- ``claude_review_synthesize`` knows about the Claude SDK runner and how to
  parse JSON from its output. It does not know about ``ReviewResponse``,
  hallucination filtering, or ``ReviewContext``.
- ``assemble_review_from_synthesis`` knows about the domain
  (``ReviewResponse``, hallucination filter, ``ReviewContext``). It does
  not know about runners or JSON parsing.

Each stage stays single-responsibility and individually testable.

## Failure policy

``on_failure="abort"`` at the pipeline level — if synthesis produced no
parseable payload, there's nothing to publish and the run should fail
loudly. The narrativize/publish chain can technically tolerate an empty
``ReviewResponse`` (it's what the monolith falls back to on
``ValidationError``), but for the multi-perspective pipeline we prefer the
explicit abort: the adversarial pair claimed success but produced empty
output, that's a bug worth surfacing.

The narrower fallback (Codex unavailable → claude_only) already lives
inside ``claude_review_synthesize``. By the time we get here, that path
has been exhausted.
"""

from __future__ import annotations

import json
from typing import Any

from ..log import logger
from ..orchestration.stage import StageFn


def make_assemble_review_from_synthesis_stage() -> StageFn:
    """Factory: returns the stage function.

    Factory wrapper for consistency with the other multi-perspective stages,
    even though this stage has no injected dependencies. Future variations
    (e.g. a stage that consumes a different schema, or a stage that emits
    additional telemetry) can be parameterized through the factory without
    changing the pipeline wiring.
    """

    def assemble_review_from_synthesis(
        ctx: Any, prev: dict[str, Any]
    ) -> dict[str, Any]:
        """Convert the synthesizer payload into the generate_review shape."""
        from ..domain.exceptions import ValidationError
        from ..domain.models import ReviewContext, ReviewResponse
        from ..infra.constants import REVIEW_JSON

        synth_out = prev.get("claude_review_synthesize", {}) or {}
        if not isinstance(synth_out, dict):
            synth_out = {}

        payload = synth_out.get("synthesized_payload", {}) or {}
        succeeded = bool(synth_out.get("succeeded"))
        parse_error = synth_out.get("parse_error")
        mode = synth_out.get("mode", "unknown")

        # Diff comes from the upstream diff stage (same as generate_review_stage)
        diff_out = prev.get("diff", {})
        diff_text = diff_out.get("diff", "") if isinstance(diff_out, dict) else ""
        truncated = bool(diff_out.get("truncated", False)) if isinstance(diff_out, dict) else False

        rules_out = prev.get("rules", {}) or {}
        rules = rules_out.get("rules", "") if isinstance(rules_out, dict) else ""
        learnings = rules_out.get("learnings", "") if isinstance(rules_out, dict) else ""
        # ReviewContext.selected_rules is str — upstream may legacy-pass list
        _sr = rules_out.get("selected_rules", "") if isinstance(rules_out, dict) else ""
        if isinstance(_sr, str):
            selected_rules = _sr
        elif isinstance(_sr, list):
            selected_rules = "\n".join(str(r) for r in _sr)  # type: ignore[unreachable]
        else:
            selected_rules = str(_sr)

        context_out = prev.get("context", {}) or {}
        if isinstance(context_out, dict):
            resolved_threads = context_out.get("resolved_threads", "")
            prior_art = context_out.get("prior_art", "")
            css_health = context_out.get("css_health", "")
            ci_status = context_out.get("ci_status", "")
            full_files = context_out.get("full_files", "")
        else:
            resolved_threads = prior_art = css_health = ci_status = full_files = ""

        deep_out = prev.get("deep_analysis", {}) or {}
        deep_analysis = (
            deep_out.get("deep_analysis", "") if isinstance(deep_out, dict) else ""
        )

        # Synthesizer failure is a HARD failure — DO NOT fall back to empty
        # review. The previous behavior (silently producing
        # ReviewResponse.empty()) caused the audit Finding 2 incident on
        # https://github.com/bernard-org/bernard-org-gen-backend/pull/1364: the
        # pipeline cheerfully published "Review produced invalid output.
        # 0 findings." while the actual PR contained 4 critical bugs.
        #
        # Raise instead. The Executor catches the exception and applies
        # this stage's on_failure="abort" policy: downstream narrativize +
        # publish are skipped. The workflow run UI shows the failure
        # honestly; no false "Clean pass" hits production.
        #
        # The narrower fallback inside claude_review_synthesize (Codex
        # unavailable → claude_only) still works — that's a synth-internal
        # path that produces a real payload, not what we're catching here.
        review: ReviewResponse
        if not succeeded or not payload:
            from ..domain.exceptions import AIReviewerError

            reason = parse_error or "synthesizer did not succeed"
            raise AIReviewerError(
                f"Synthesizer produced no usable payload: {reason}. "
                f"Mode was {mode}. The adversarial pair cannot publish a "
                "review without synthesized content; this run is aborted "
                "instead of silently posting empty findings."
            )
        try:
            review = ReviewResponse.validate(payload)
        except ValidationError as e:
            from ..domain.exceptions import AIReviewerError

            raise AIReviewerError(
                f"Synthesizer payload failed validation: {e}"
            ) from e

        # Hallucination filter — drop findings citing files not in the diff.
        # Even after Claude validates against source, this catches cases
        # where Claude got the file path subtly wrong (e.g. wrong casing).
        review, dropped_count = _filter_findings_against_diff(review, diff_text)

        # Build the ReviewContext the rest of the pipeline expects.
        # We don't have a system_prompt / user_message here (no build_prompt
        # call was made), but narrativize + write_trace only read .review
        # and the gather outputs.
        review_ctx = ReviewContext(
            pr=ctx.pr,
            model=getattr(ctx, "model", ""),
            max_diff_bytes=getattr(ctx, "max_diff_bytes", 0),
            prompt_file="multi-perspective",
            diff=diff_text,
            truncated=truncated,
            learnings=learnings,
            rules=rules,
            selected_rules=selected_rules,
            resolved_threads=resolved_threads,
            prior_art=prior_art,
            css_health=css_health,
            ci_status=ci_status,
            deep_analysis=deep_analysis,
            full_files=full_files,
            review=review,
        )

        # Persist the synthesizer's raw payload for trace inspection
        # (mirrors generate_review_stage writing the raw LLM output).
        try:
            ctx.store.write(REVIEW_JSON, json.dumps(payload, indent=2))
        except Exception:
            pass  # non-fatal

        return {
            "review": review,
            "raw": payload,
            "dropped_count": dropped_count,
            "review_ctx": review_ctx,
            "synthesizer_mode": mode,
        }

    return assemble_review_from_synthesis


# -- Helpers --------------------------------------------------------------


def _filter_findings_against_diff(review, diff_text: str):
    """Drop findings referencing files not in the diff. Duplicated from
    review_bridge to keep this stage self-contained — small enough that
    centralizing it would add a dependency layer without saving real code."""
    from ..domain.models import ReviewResponse

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
            logger.warning(
                f"::warning::Dropped hallucinated finding for {f.file} (not in diff)"
            )
            dropped += 1
            continue
        valid_findings.append(f)

    if dropped > 0:
        return (
            ReviewResponse(
                title=review.title,
                summary=review.summary,
                findings=valid_findings,
                highlights=review.highlights,
                knowledge=review.knowledge,
            ),
            dropped,
        )
    return review, 0


def _warn(ctx: Any, msg: str) -> None:
    """Surface a warning through ctx.actions if available, else logger."""
    actions = getattr(ctx, "actions", None)
    if actions is not None and hasattr(actions, "warning"):
        try:
            actions.warning(msg)
            return
        except Exception:
            pass
    logger.warning(msg)

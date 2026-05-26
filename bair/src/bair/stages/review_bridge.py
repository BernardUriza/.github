"""F-bridge stages — adapta los gatherers del review pipeline al patrón Stage.

Primera demostración real de la fundación F1-F4: convierte 4 fases del review
pipeline (dedup, diff, rules, context) en Stages composables que el Executor
corre como DAG declarativo.

Scope intencional:

- NO reemplaza `pipelines/review.py` original (sigue siendo el flow productivo)
- SÍ provee un Pipeline paralelo que demuestra que el Executor maneja el caso
- Cada stage es una función pura con firma StageFn — tipada contra Protocols
  de `contracts/`, sin importar `infra/` ni `pipelines/`

Las 4 fases cubiertas (de las 7 originales):

1. `dedup_check`  — check_existing_review → early decision (no aborta el run)
2. `gather_diff`  — fetch PR diff con truncation
3. `gather_rules` — gather + condense (incluye una llamada LLM)
4. `gather_context` — bundle de threads/prior_art/css/ci/files

Stages 5-7 (deep_analysis, generate_review, publish) quedan para una iteración
siguiente — necesitan más glue del ReviewContext y la decisión de cómo manejar
narrativize/validate sin acoplar el Executor al domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..contracts import ActionsIO, FileStore, GitHubClient, LlmProvider
from ..domain.models import PRIdentifier
from ..gatherers.ci_status import gather_ci_status
from ..gatherers.css_health import gather_css_health
from ..gatherers.diff import gather_diff as _gather_diff
from ..gatherers.file_context import gather_file_context
from ..gatherers.learnings import gather_learnings
from ..gatherers.prior_art import gather_prior_art
from ..gatherers.rules import condense_rules, gather_rules
from ..gatherers.threads import gather_threads
from ..domain.publishing import check_existing_review
from ..log import logger


@dataclass(frozen=True, slots=True)
class ReviewBridgeContext:
    """Input compartido a todos los stages del review pipeline.

    Tipado contra Protocols de `contracts/` — los stages no necesitan saber
    si el `github` es `HttpGitHubClient` o un fake en tests.
    """

    pr: PRIdentifier
    github: GitHubClient
    store: FileStore
    actions: ActionsIO
    llm: LlmProvider
    # Subset de ReviewConfig usado por los stages — el caller hace el unpacking
    max_diff_bytes: int
    dry_run: bool
    skip_dedup: bool
    variant: str
    # F-bridge v2 fields — required by deep_analysis / generate / narrativize / publish
    # Defaults preserve backward compat with v1 prep-only tests that don't need them.
    cfg: Any = None        # ReviewConfig — opaque to stages; used by deep_analysis
    container: Any = None  # Container — opaque; used by deep_analysis (get_deep_llm)
    model: str = ""
    max_tokens: int = 4000
    temperature: float = 0.3
    run_id: str = "local"


# -- Stage functions -------------------------------------------------------


def dedup_check(ctx: ReviewBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """¿Ya existe review para el último commit?

    Si sí, retorna ``should_skip=True`` con razón. Los stages downstream
    deciden qué hacer con esa señal — este stage NUNCA aborta el pipeline
    (un dedup positivo es una decisión, no un error).

    Para dry-run y skip-dedup explícito, devolvemos ``should_skip=False``.
    """
    if ctx.dry_run or ctx.skip_dedup:
        return {"should_skip": False, "reason": "skipped by config"}
    if check_existing_review(ctx.github, ctx.pr):
        return {
            "should_skip": True,
            "reason": "duplicate review exists for latest commit",
        }
    return {"should_skip": False, "reason": ""}


def gather_diff_stage(
    ctx: ReviewBridgeContext, prev: dict[str, Any]
) -> dict[str, Any]:
    """Fetch del diff de la PR. Stage abort-on-failure — sin diff no hay review.

    Honra el resultado del dedup_check upstream: si dedup decidió skip,
    devuelve un diff vacío sin gastar llamadas a la API de GitHub.
    """
    if prev.get("dedup", {}).get("should_skip"):
        return {"diff": "", "truncated": False, "lines": 0, "skipped": True}
    diff, truncated = _gather_diff(
        ctx.pr, ctx.max_diff_bytes, ctx.github, ctx.store, ctx.actions
    )
    return {
        "diff": diff,
        "truncated": truncated,
        "lines": diff.count("\n"),
        "skipped": False,
    }


def gather_rules_stage(
    ctx: ReviewBridgeContext, prev: dict[str, Any]
) -> dict[str, Any]:
    """Fetch raw rules + learnings, luego condensa via LLM.

    Política ``on_failure="continue"``: si la condensación falla, el review
    procede con rules raw. Mejor un review con rules sin condensar que ninguno.
    """
    if prev.get("dedup", {}).get("should_skip"):
        return {"rules": "", "learnings": "", "selected_rules": [], "skipped": True}

    learnings = (
        gather_learnings(ctx.variant, ctx.store, ctx.actions) if ctx.variant else ""
    )
    raw_rules, selected_rules = gather_rules(
        ctx.pr, ctx.github, ctx.store, ctx.actions
    )
    condensed = condense_rules(raw_rules, ctx.llm, ctx.actions)
    return {
        "rules": condensed,
        "learnings": learnings,
        "selected_rules": selected_rules,
        "raw_rules_size": len(raw_rules),
        "skipped": False,
    }


def gather_context_stage(
    ctx: ReviewBridgeContext, prev: dict[str, Any]
) -> dict[str, Any]:
    """Bundle de los context gatherers — threads, prior_art, CSS, CI, files.

    Cada gatherer ya tiene fallback interno; este stage los compone. Si TODOS
    fallaran, el review correría con context vacío (degraded pero usable).
    Por eso debería declararse como ``on_failure="continue"``.

    El diff (output de ``gather_diff_stage``) es input para algunos sub-gatherers
    (prior_art usa el diff para detectar archivos a comparar). Lo leemos de
    ``prev``.
    """
    diff_result = prev.get("diff", {})
    if diff_result.get("skipped"):
        return {
            "resolved_threads": "",
            "prior_art": "",
            "css_health": "",
            "ci_status": "",
            "full_files": "",
            "skipped": True,
        }

    diff_text = diff_result.get("diff", "")

    return {
        "resolved_threads": gather_threads(
            ctx.pr, ctx.github, ctx.store, ctx.actions
        ),
        "prior_art": gather_prior_art(
            ctx.pr.repo, diff_text, ctx.github, ctx.store, ctx.actions
        ),
        "css_health": gather_css_health(
            ctx.pr, ctx.github, ctx.store, ctx.actions
        ),
        "ci_status": gather_ci_status(
            ctx.pr, ctx.github, ctx.store, ctx.actions
        ),
        "full_files": gather_file_context(
            ctx.pr, ctx.github, ctx.store, ctx.actions
        ),
        "skipped": False,
    }


# -- F-bridge v2 stages — deep_analysis, generate, narrativize, publish ----


def deep_analysis_stage(
    ctx: ReviewBridgeContext, prev: dict[str, Any]
) -> dict[str, Any]:
    """Run optional deep analysis via Claude Agent SDK (DeepMode-gated).

    Wraps ``pipelines.review.resolve_deep_analysis``. Politica
    ``on_failure="continue"`` — si Claude SDK falla, el review sigue con
    GPT-only (mismo comportamiento que el monolito).

    Returns dict con ``deep_analysis: str`` (vacío si DISABLED o falla).
    """
    diff_result = prev.get("diff", {})
    if diff_result.get("skipped"):
        return {"deep_analysis": "", "skipped": True}

    if ctx.cfg is None or ctx.container is None:
        # F-bridge v1 callers (prep-only) no provide cfg/container — skip
        return {"deep_analysis": "", "skipped": True, "reason": "no cfg/container"}

    # Lazy import — resolve_deep_analysis lives in pipelines/review.py
    # (whitelisted in test_layer_purity: stages → pipelines.review)
    from ..pipelines.review import resolve_deep_analysis

    diff_text = diff_result.get("diff", "")
    deep_analysis = resolve_deep_analysis(ctx.cfg, diff_text, ctx.container)
    return {"deep_analysis": deep_analysis, "skipped": False}


def generate_review_stage(
    ctx: ReviewBridgeContext, prev: dict[str, Any]
) -> dict[str, Any]:
    """Build prompt + call LLM + validate + filter hallucinated findings.

    Heaviest stage of the pipeline — the actual GPT review call. Politica
    ``on_failure="abort"`` — sin review no hay nada que publicar.

    Returns dict con:
        - review: ReviewResponse (validated, hallucinated-stripped)
        - raw: dict (the LLM's raw output)
        - dropped_count: int (findings filtered out as hallucinated)
        - ctx_updated: ReviewContext con todos los outputs upstream merged
    """
    from dataclasses import replace
    import json

    from ..domain.exceptions import ValidationError
    from ..domain.models import ReviewContext, ReviewResponse
    from ..infra.constants import REVIEW_JSON
    from ..prompt.builder import build_prompt

    diff_result = prev.get("diff", {})
    diff_text = diff_result.get("diff", "")
    truncated = bool(diff_result.get("truncated", False))

    rules_result = prev.get("rules", {})
    rules = rules_result.get("rules", "")
    learnings = rules_result.get("learnings", "")
    selected_rules = rules_result.get("selected_rules", [])

    context_result = prev.get("context", {})
    resolved_threads = context_result.get("resolved_threads", "")
    prior_art = context_result.get("prior_art", "")
    css_health = context_result.get("css_health", "")
    ci_status = context_result.get("ci_status", "")
    full_files = context_result.get("full_files", "")

    deep_result = prev.get("deep_analysis", {})
    deep_analysis = deep_result.get("deep_analysis", "")

    # Build ReviewContext to feed build_prompt
    review_ctx = ReviewContext(
        pr=ctx.pr,
        model=ctx.model,
        max_diff_bytes=ctx.max_diff_bytes,
        prompt_file=f"prompts/{ctx.variant}.md" if ctx.variant else "prompts/frontend.md",
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
    )
    review_ctx = build_prompt(review_ctx)

    # LLM call
    raw = ctx.llm.call(
        system=review_ctx.system_prompt,
        user=review_ctx.user_message,
        model=ctx.model,
        max_tokens=ctx.max_tokens,
        temperature=ctx.temperature,
    )

    # Validate + guardrail
    try:
        review = ReviewResponse.validate(raw)
    except ValidationError as e:
        ctx.actions.warning(f"LLM output validation failed: {e}")
        review = ReviewResponse.empty()

    # Filter hallucinated findings (files not in diff)
    review, dropped_count = _filter_findings_against_diff(review, diff_text)
    review_ctx = replace(review_ctx, review=review)

    # Persist raw output for trace
    try:
        ctx.store.write(REVIEW_JSON, json.dumps(raw, indent=2))
    except Exception:
        pass  # non-fatal

    return {
        "review": review,
        "raw": raw,
        "dropped_count": dropped_count,
        "review_ctx": review_ctx,
    }


def _filter_findings_against_diff(review, diff_text: str):
    """Drop findings referencing files not in the diff. Mirrors
    pipelines/review._validate_findings_against_diff but inlined here so
    stages/review_bridge doesn't pull more from pipelines/.
    """
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


def narrativize_stage(
    ctx: ReviewBridgeContext, prev: dict[str, Any]
) -> dict[str, Any]:
    """Transform the structured review into BAIR's narrative voice.

    Wraps ``domain.narrator.narrativize``. Politica ``on_failure="continue"``
    — sin narrative el publisher cae al ``review.summary`` plain.
    """
    from ..domain.narrator import narrativize

    generate_result = prev.get("generate_review", {})
    review = generate_result.get("review")
    if review is None:
        return {"narrative": "", "skipped": True}

    narrative = narrativize(review, ctx.llm)
    return {"narrative": narrative, "skipped": False}


def publish_review_stage(
    ctx: ReviewBridgeContext, prev: dict[str, Any]
) -> dict[str, Any]:
    """Publish the review via the appropriate publisher.

    Selects DryRunPublisher when ``ctx.dry_run`` else GitHubPublisher.
    Mirrors pipelines/review._make_publisher inline. Politica
    ``on_failure="abort"`` — failed publish means the review never made it
    to the PR, so the run is functionally useless.
    """
    from ..domain.publishing import DryRunPublisher, GitHubPublisher

    generate_result = prev.get("generate_review", {})
    review = generate_result.get("review")
    raw = generate_result.get("raw", {})
    dropped_count = int(generate_result.get("dropped_count", 0) or 0)
    if review is None:
        raise ValueError(
            "publish_review_stage requires generate_review.review in prev"
        )

    diff_result = prev.get("diff", {})
    diff_text = diff_result.get("diff", "")
    truncated = bool(diff_result.get("truncated", False))

    narrative_result = prev.get("narrativize", {})
    narrative = narrative_result.get("narrative", "")

    if ctx.dry_run:
        publisher = DryRunPublisher()
    else:
        publisher = GitHubPublisher(github=ctx.github, actions=ctx.actions)

    publisher.publish(
        review,
        ctx.pr,
        ctx.run_id,
        truncated,
        ctx.max_diff_bytes,
        dropped_count,
        narrative=narrative,
        diff_text=diff_text,
    )
    return {
        "published": True,
        "dry_run": ctx.dry_run,
        "findings_count": len(review.findings),
        "narrative_len": len(narrative),
    }

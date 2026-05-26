"""F-bridge v2 resolve — post-agent stages.

Completa el flow del resolve pipeline después de que el agente corrió y el
security_pass aplicó gating. Cinco stages mecánicos + un gating stage:

1. ``assert_agent_succeeded`` — gate: si ``outcome.error`` está set, raise.
   Convierte el "stage observa, gate enforza" del agent_run en abort real.
2. ``substance_gate`` — wrap de ``check_substance_gate`` (rechaza pushes
   cuando el agente no produjo cambios sustanciales).
3. ``verify_build`` — wrap de ``verify_build`` (corre ``npx tsc --noEmit``).
4. ``commit_push`` — wrap de ``commit_and_push`` (git add + commit + push).
5. ``create_pr`` — wrap de ``create_pr`` (gh pr create draft).
6. ``update_tracker`` — inline: actualiza Plane/Linear con PR URL +
   transición a "In Review".

Cada wrapper extrae los inputs del ``ResolveBridgeContext`` + ``prev`` dict y
delega al helper. Mantenemos las firmas de los helpers intactas (después de
la promoción de ``_private`` → public) para que ``pipelines/resolve.py`` siga
usándolos in-place mientras el bridge madura.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.exceptions import ProviderError
from ..domain.models import Issue
from ..log import logger
from ..pipelines.resolve import check_substance_gate, commit_and_push, create_pr, verify_build
from .resolve_bridge import ResolveBridgeContext


@dataclass(frozen=True, slots=True)
class _PrCreationConfig:
    """Mínimo subset de ResolveConfig que ``create_pr`` necesita.

    ``create_pr`` espera un ``cfg`` con ``.repo`` y ``.base_branch``. Para
    no acoplar el stage a ``config.ResolveConfig`` entero, construimos este
    adapter mínimo desde el ``ResolveBridgeContext``.
    """

    repo: str
    base_branch: str


# -- Helper: extract Issue from prev ---------------------------------------


def _issue_from_prev(prev: dict[str, Any]) -> Issue:
    """Levanta ProviderError si no encuentra el Issue del fetch_issue stage."""
    fetched = prev.get("fetch_issue", {})
    if isinstance(fetched, dict):
        issue = fetched.get("issue")
        if isinstance(issue, Issue):
            return issue
    raise ProviderError(
        "post-agent stage requires fetch_issue.issue in prev — "
        "fetch_issue likely failed or was skipped"
    )


# -- Gating stage: assert agent succeeded ---------------------------------


def assert_agent_succeeded(ctx: ResolveBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """Gate: si ``prev["agent_run"]["error"]`` está set, raise.

    Convierte la decisión "stage observa, gate enforza" del agent_run en un
    abort real del pipeline. Stages downstream (substance, verify, push, PR)
    se marcan SKIPPED automáticamente cuando este raise.

    Política: ``on_failure="abort"``. Sin trabajo del agente no hay nada que
    validar ni publicar.

    Output cuando OK: dict con ``{"agent_succeeded": True}`` (señal explícita
    para auditoría del trace, aunque downstream no consuma).
    """
    agent_output = prev.get("agent_run", {})
    if not isinstance(agent_output, dict):
        raise ProviderError(
            "assert_agent_succeeded requires agent_run output in prev"
        )

    error = agent_output.get("error")
    if error:
        # Levantamos como ProviderError (consistente con run_resolve original)
        raise ProviderError(f"Agent SDK failed: {error}")

    return {"agent_succeeded": True}


# -- substance_gate stage --------------------------------------------------


def substance_gate(ctx: ResolveBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """Refuse to push if agent did no substantive Edit/Write work.

    Wraps ``pipelines.work.check_substance_gate``. Inputs:
    - ``prev["agent_run"]["edit_calls"]`` — count del agente
    - ``prev["fetch_issue"]["identifier"]`` — issue id para el error message
    - ``ctx.workspace``, ``ctx.base_branch`` — git context

    Política: ``on_failure="abort"`` — junk PRs son worse que no PRs.
    """
    agent_output = prev.get("agent_run", {})
    edit_calls = int(agent_output.get("edit_calls", 0) or 0) if isinstance(agent_output, dict) else 0

    # Pull tool_breakdown from the outcome stored by stages/agent_run.py so
    # check_substance_gate can enforce "investigation evidence before Edit"
    # (VIS-296 / PR #536 failure mode). When outcome is missing the gate
    # falls back to the legacy edit_calls-only check.
    outcome = agent_output.get("outcome") if isinstance(agent_output, dict) else None
    tool_breakdown: tuple[tuple[str, int], ...] = ()
    if outcome is not None and hasattr(outcome, "tool_breakdown"):
        tool_breakdown = outcome.tool_breakdown

    fetched = prev.get("fetch_issue", {})
    issue_id = (
        fetched.get("identifier", ctx.issue_id)
        if isinstance(fetched, dict)
        else ctx.issue_id
    )

    check_substance_gate(
        edit_calls=edit_calls,
        workspace=ctx.workspace,
        base_branch=ctx.base_branch,
        issue_id=issue_id,
        tool_breakdown=tool_breakdown,
    )
    return {"passed": True, "edit_calls": edit_calls}


# -- verify_build stage ----------------------------------------------------


def verify_build_stage(ctx: ResolveBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """Run ``npx tsc --noEmit`` as a post-agent type-safety gate.

    Wraps ``pipelines.work.verify_build``. Política: ``on_failure="abort"`` —
    no merge code that doesn't type-check.

    NOTA: el orden en el monolito original es commit → push → verify (verify
    after push para preservar el trabajo aunque tsc falle). En el pipeline
    declarativo seguimos el mismo orden — el caller decide la posición de
    este stage relativa a commit_push.
    """
    if not ctx.repo:
        raise ProviderError(
            "verify_build_stage requires ctx.repo (e.g. 'owner/name')"
        )
    verify_build(ctx.github, ctx.workspace, ctx.repo)
    return {"passed": True}


# -- commit_push stage -----------------------------------------------------


def commit_push(ctx: ResolveBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """Stage, commit, push to origin/<issue.git_branch_name>.

    Wraps ``pipelines.work.commit_and_push``. Política: ``on_failure="continue"``
    — siguiendo el monolito original, si push falla no bloqueamos el trace
    write (la artifact upload del workflow es la safety net last-resort).
    """
    issue = _issue_from_prev(prev)
    commit_and_push(ctx.github, ctx.workspace, issue)
    return {
        "pushed": True,
        "branch": issue.git_branch_name,
    }


# -- create_pr stage -------------------------------------------------------


def create_pr_stage(ctx: ResolveBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """Create a draft PR via gh CLI.

    Wraps ``pipelines.work.create_pr``. Construye un adapter ``_PrCreationConfig``
    desde el ctx para no acoplar el stage a ``config.ResolveConfig`` entero.

    Threads ``prev["agent_run"]["outcome"]`` into ``create_pr`` so the PR body
    cites the agent's actual narration + files_touched instead of just
    paraphrasing the issue description. Falls back to the legacy body shape
    if the outcome is missing (e.g. older stage configurations).

    Política: ``on_failure="abort"`` — sin PR el flow no se cierra.
    """
    issue = _issue_from_prev(prev)
    if not ctx.repo:
        raise ProviderError("create_pr_stage requires ctx.repo (e.g. 'owner/name')")

    agent_output = prev.get("agent_run", {})
    outcome = (
        agent_output.get("outcome")
        if isinstance(agent_output, dict)
        else None
    )

    cfg_adapter = _PrCreationConfig(repo=ctx.repo, base_branch=ctx.base_branch)
    pr_url = create_pr(ctx.github, issue, cfg_adapter, ctx.workspace, outcome=outcome)  # type: ignore[arg-type]
    return {"pr_url": pr_url}


# -- update_tracker stage --------------------------------------------------


def update_tracker(ctx: ResolveBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """Update issue tracker (Plane/Linear) with the PR URL + transition state.

    Inline implementation (3 calls to ``IssueTrackerClient`` methods). No
    wrapper of a private helper — la lógica es trivial y vive en stages/.

    Política: ``on_failure="continue"`` — tracker update failing should NOT
    block the run from completing (PR already created, code already pushed).
    Logs the warning and returns ``updated=False`` so the trace shows it.
    """
    issue = _issue_from_prev(prev)
    pr_output = prev.get("create_pr_stage", prev.get("create_pr", {}))
    pr_url = (
        pr_output.get("pr_url", "") if isinstance(pr_output, dict) else ""
    )
    if not pr_url:
        logger.warning("[TRACKER] No pr_url in prev — skipping tracker update")
        return {"updated": False, "reason": "no pr_url"}

    try:
        ctx.issue_tracker.add_attachment(issue.id, pr_url, f"PR: {issue.identifier}")
        ctx.issue_tracker.add_comment(
            issue.id,
            f"Automated PR created: [{pr_url}]({pr_url})",
        )
        ctx.issue_tracker.transition_to(issue, "started")
        return {"updated": True, "pr_url": pr_url}
    except Exception as e:
        logger.warning(f"[TRACKER] Update failed (non-fatal): {e}")
        return {"updated": False, "reason": str(e)}

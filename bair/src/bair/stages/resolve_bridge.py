"""F-bridge stages — pre-agent phases del resolve pipeline.

Segundo consumer real de la fundación F1-F4 (el primero fue `review_bridge`).
Convierte las 3 primeras fases del resolve pipeline (fetch issue, create branch,
build prompt) en Stages composables.

Scope intencional:

- NO reemplaza `pipelines/resolve.py` original (sigue siendo el flow productivo)
- SÍ provee un Pipeline paralelo que demuestra que el Executor maneja el
  caso más complejo: stages que tocan **issue tracker** (Linear/Plane) y
  **git operations**, no solo gatherers de diff/rules
- Cada stage tipa contra Protocols de `contracts/` — los tests inyectan fakes
  de `IssueTrackerClient` y `GitHubClient` sin tocar `infra/`

Las 3 fases cubiertas (de las 8 originales — ver `pipelines/resolve.py`):

1. `fetch_issue`     — issue tracker call + validation (git_branch_name presente)
2. `create_branch`   — git fetch + checkout, con fallback a branch existente
3. `build_prompt`    — wrap de `domain.resolve_prompts.build_resolve_prompt`

Fases 4-8 (agent SDK run, substance gate, verify build, commit + push, create
PR, update tracker) quedan para una iteración siguiente — la fase 4 requiere
extraer la invocación async de `claude_agent_sdk` del monolito de `run_resolve`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..contracts import ActionsIO, AgentRunner, GitHubClient, IssueTrackerClient
from ..domain.agent_run import AgentRunOutcome
from ..domain.exceptions import ConfigError
from ..domain.models import Issue
from ..domain.resolve_prompts import RESOLVE_SYSTEM_PROMPT, build_resolve_prompt
from ..log import logger
from ..policies.security import DiffSignals


# Scanner callable — matches stages/security_pass.py ScannerFn signature.
# Defined locally so this module doesn't import from another stages/ module
# (lateral imports between stage modules are allowed but kept minimal).
SecurityScannerFn = Callable[[DiffSignals], list[dict[str, Any]]]


def _default_security_scanner(signals: DiffSignals) -> list[dict[str, Any]]:
    """Placeholder scanner used when ctx.security_scanner not provided.

    Returns no findings — gate will pass. F-future replaces with a real
    bandit/semgrep wrapper. Same shape as `stages.security_pass.noop_scanner`.
    """
    return []


@dataclass(frozen=True, slots=True)
class ResolveBridgeContext:
    """Input compartido a los stages del resolve pipeline.

    Tipado contra Protocols de `contracts/` — fakes en tests no necesitan
    instanciar implementaciones de `infra/`.

    Extended in F-bridge v2 resolve with fields for agent_run + security_pass
    stages. Older fields keep their semantics; new fields have defaults so
    tests of the prep-only pipeline (fetch_issue + create_branch + build_prompt)
    don't need to supply them.
    """

    issue_id: str
    base_branch: str
    workspace: str  # path al checkout local del repo target
    issue_tracker: IssueTrackerClient
    github: GitHubClient
    actions: ActionsIO
    # Target repo en formato "owner/name" (used by create_pr stage)
    repo: str = ""
    # Agent run stage inputs (defaults for prep-only pipelines)
    system_prompt: str = RESOLVE_SYSTEM_PROMPT
    max_turns: int = 12
    agent_runner: AgentRunner | None = None
    # Security pass stage inputs
    security_scanner: SecurityScannerFn = _default_security_scanner


# -- Stage functions -------------------------------------------------------


def fetch_issue(ctx: ResolveBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """Lee la issue del tracker (Linear o Plane) y valida que esté lista.

    Política ``on_failure="abort"`` — sin issue válida no hay nada que hacer.
    Las validaciones se ejecutan acá para que el fallo tenga contexto:
    qué falta exactamente.

    Returns dict con:
        - issue: el objeto Issue
        - identifier: <tracker-prefix>-NNN
        - git_branch_name: validado no-vacío
        - title, project_name, assignee_name, labels
    """
    issue: Issue = ctx.issue_tracker.get_issue(ctx.issue_id)

    if not issue.git_branch_name:
        raise ConfigError(
            f"Issue {issue.identifier} has no git_branch_name in tracker — "
            "cannot proceed without a branch name"
        )

    return {
        "issue": issue,
        "identifier": issue.identifier,
        "git_branch_name": issue.git_branch_name,
        "title": issue.title,
        "state_name": issue.state_name,
        "project_name": issue.project_name or "",
        "assignee_name": issue.assignee_name or "",
        "labels": tuple(issue.labels) if issue.labels else (),
    }


def create_branch(ctx: ResolveBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """Fetch origin y crea (o checkout) la branch de la issue desde base_branch.

    Política ``on_failure="abort"`` — sin branch no puede correr el agent.

    Maneja el caso "branch ya existe": si `checkout -b` falla, fallback a
    `checkout` simple. Mismo comportamiento que el `run_resolve` monolítico,
    extraído como stage.

    Returns dict con:
        - branch: nombre de la branch ahora activa
        - created: True si la creamos, False si ya existía
        - base_branch: la base de origen
    """
    fetched_issue = prev.get("fetch_issue", {})
    branch = fetched_issue.get("git_branch_name", "")
    if not branch:
        # Defensive — el orden topológico garantiza que fetch_issue corrió
        # antes que create_branch, pero si fetch_issue fue skipped por
        # fallo upstream, no hay branch que crear.
        raise ConfigError(
            "create_branch stage called without git_branch_name in prev — "
            "fetch_issue likely failed or was skipped"
        )

    ctx.github.run_git("fetch", "origin", cwd=ctx.workspace)
    created = True
    try:
        ctx.github.run_git(
            "checkout", "-b", branch,
            f"origin/{ctx.base_branch}",
            cwd=ctx.workspace,
        )
        logger.info(f"  created branch {branch} from origin/{ctx.base_branch}")
    except Exception:
        # Branch existed already — checkout sin -b
        ctx.github.run_git("checkout", branch, cwd=ctx.workspace)
        logger.info(f"  checked out existing branch {branch}")
        created = False

    return {
        "branch": branch,
        "created": created,
        "base_branch": ctx.base_branch,
    }


def build_prompt(ctx: ResolveBridgeContext, prev: dict[str, Any]) -> dict[str, Any]:
    """Construye el user prompt para Claude Agent SDK a partir de la issue.

    Pure function — no side effects, no I/O. Wrap delgado de
    `domain.resolve_prompts.build_resolve_prompt`.

    Política ``on_failure="abort"`` — sin prompt no se puede invocar al agente.

    Returns dict con:
        - prompt: el user message para el agente
        - length: caracteres (para tracing/budget checks futuros)
    """
    fetched_issue = prev.get("fetch_issue", {})
    issue: Issue | None = fetched_issue.get("issue")
    if issue is None:
        raise ConfigError(
            "build_prompt stage called without issue in prev — "
            "fetch_issue likely failed or was skipped"
        )

    prompt_text = build_resolve_prompt(issue, ctx.base_branch)
    return {
        "prompt": prompt_text,
        "length": len(prompt_text),
    }


def derive_diff_signals(
    ctx: ResolveBridgeContext, prev: dict[str, Any]
) -> dict[str, Any]:
    """Heurística: deriva DiffSignals del agent run outcome.

    En F-future un stage dedicado correrá ``git diff --name-only`` post-agent
    y clasificará archivos en categorías de riesgo (helpers, config, auth,
    input-handling) mirando paths. Por ahora, derivamos signals a partir de
    lo que el agente HIZO (no de los archivos finales):

    - ``edit_calls > 0``: proxy de "tocó helpers/util" — heurística amplia
    - ``destructive_calls > 0``: señal explícita de "tocó config" (npm install,
      etc., son por definición config-mutating)

    Es heurística amplia — prefiere FAIL-OPEN (correr scanner) sobre FAIL-CLOSED.
    El gate de severity downstream (gate_security en policies/) decide qué
    bloquea y qué pasa, así que la heurística amplia solo afecta cuánto corre
    el scanner, no qué bloquea.

    Returns dict con:
        - signals: DiffSignals derivado
        - source: "agent_run_outcome" (audit trail)
    """
    agent_output = prev.get("agent_run", {})

    if not isinstance(agent_output, dict):
        # Agent stage no corrió o crasheó sin output — signals vacíos.
        return {"signals": DiffSignals(), "source": "no agent output"}

    edit_calls = int(agent_output.get("edit_calls", 0) or 0)
    destructive_count = len(agent_output.get("destructive_calls", ()) or ())

    signals = DiffSignals(
        helpers_touched=edit_calls,  # heurística amplia
        config_touched=1 if destructive_count > 0 else 0,
    )
    return {"signals": signals, "source": "agent_run_outcome"}

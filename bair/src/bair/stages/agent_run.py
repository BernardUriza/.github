"""Agent run stage — invoca un agente LLM con tool-use via AgentRunner Protocol.

Plan v7 sección 2 (Two-commit synthesis). En el resolve pipeline el agent_run
es el corazón: genera commits que después un Claude synthesizer valida.
En F-bridge v2 resolve este stage será el primer consumer real del Protocol.

## Wiring de AgentRunner Protocol

Este es el primer stage que enchufa la abstracción `contracts/AgentRunner`
al patrón de stages:

```python
stage = make_agent_run_stage(runner=ClaudeSDKAgentRunner(max_turns=12))
Pipeline(stages=(..., Stage(name="agent_run", fn=stage), ...))
```

El stage es agnóstico a qué proveedor concreto corre — Claude SDK, Codex
CLI, o un fake en tests. Eso vive detrás del Protocol.

## Por qué factory pattern

Mismo razonamiento que `security_pass`:

- Tests inyectan FakeAgentRunner que retorna AgentRunOutcome programado
- Producción inyecta ClaudeSDKAgentRunner (F-future, no en este PR)
- Distintos pipelines pueden inyectar distintos runners

## Inputs del prev

El stage espera en `prev`:
- `build_prompt`: dict con `prompt` (el user prompt para el agente)

Y del `ctx`:
- `workspace`: directorio donde el agente opera (cwd para el SDK)
- `system_prompt`: el sistema prompt (puede venir del context o del stage upstream)
- `max_turns`: límite de turns del agente

## Output shape

Diccionario con campos de AgentRunOutcome flatten + el outcome object para
downstream:

- `outcome`: el AgentRunOutcome completo (para stages que necesitan el shape entero)
- `succeeded`: bool — convenience para gating downstream
- `turns`, `tool_calls`, `edit_calls`: ints para reporting/trace
- `has_substance`: bool — feed inicial del substance_gate (puede ser refinado
  por una policy que cuenta archivos cambiados via git diff)
- `result_text`: lo que el agente respondió al final
- `destructive_calls`: lista de comandos peligrosos detectados (para tracing)

## NO incluido en este stage

- Re-throw del agent error — el stage NO levanta excepción cuando el agente
  crashea (eso bloquearía cleanup downstream como commit_push que preserva
  trabajo parcial). El error vive en `outcome.error`; el gating stage decide.
- Logging detallado de cada ToolUseBlock — eso vive en el AgentRunner
  implementation (cerca del SDK), no en el stage.
- Substance gate check — está en `policies/security.py` style helpers,
  consumido por un stage downstream dedicado.
"""

from __future__ import annotations

from typing import Any

from ..contracts.agent_runner import AgentRunner
from ..domain.agent_run import AgentRunOutcome
from ..orchestration.stage import StageFn


def make_agent_run_stage(runner: AgentRunner) -> StageFn:
    """Construye un StageFn que invoca el AgentRunner con prompt de prev.

    Args:
        runner: implementación de AgentRunner — Claude SDK, fake, etc.

    Returns:
        StageFn lista para construir un Stage:

        ``Stage(name="agent_run", fn=make_agent_run_stage(runner),
              depends_on=("build_prompt",))``
    """

    def agent_run(ctx: Any, prev: dict[str, Any]) -> dict[str, Any]:
        """Invoca el agente con el prompt de build_prompt + system del ctx."""
        user_prompt = _extract_user_prompt(prev)
        system_prompt = _extract_system_prompt(ctx, prev)
        cwd = _extract_workspace(ctx)
        max_turns = _extract_max_turns(ctx)

        raw_outcome = runner.run(
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            cwd=cwd,
            max_turns=max_turns,
        )

        # AgentRunner.run() devuelve `object` por contracts/ purity. Esperamos
        # AgentRunOutcome por convención documentada. Si la impl rompe el
        # contrato, fallamos ruidosamente en el isinstance.
        if not isinstance(raw_outcome, AgentRunOutcome):
            raise TypeError(
                f"AgentRunner.run() returned {type(raw_outcome).__name__}, "
                f"expected AgentRunOutcome. Implementation breaks the contract."
            )

        outcome: AgentRunOutcome = raw_outcome
        return {
            "outcome": outcome,
            "succeeded": outcome.succeeded,
            "turns": outcome.turns,
            "tool_calls": outcome.tool_calls,
            "edit_calls": outcome.edit_calls,
            "has_substance": outcome.has_substance,
            "result_text": outcome.result_text,
            "destructive_calls": outcome.destructive_calls,
            "error": outcome.error,
        }

    return agent_run


# -- Extractors ------------------------------------------------------------


def _extract_user_prompt(prev: dict[str, Any]) -> str:
    """Lee el prompt de prev — typically de build_prompt stage."""
    build_prompt = prev.get("build_prompt", {})
    if isinstance(build_prompt, dict):
        prompt = build_prompt.get("prompt")
        if isinstance(prompt, str) and prompt:
            return prompt
    # Fallback — prompt directo en prev
    direct = prev.get("prompt")
    if isinstance(direct, str) and direct:
        return direct
    raise ValueError(
        "agent_run stage requires a non-empty user prompt in "
        "prev['build_prompt']['prompt'] or prev['prompt']"
    )


def _extract_system_prompt(ctx: Any, prev: dict[str, Any]) -> str:
    """Lee el system prompt del ctx o del prev.

    Defaults a string vacío — el runner concreto (ClaudeSDKAgentRunner) puede
    tener un system prompt default si el caller no provee. Pero para work
    pipelines típicamente viene del context (cargado de domain/resolve_prompts.py).
    """
    if hasattr(ctx, "system_prompt"):
        system = ctx.system_prompt
        if isinstance(system, str):
            return system
    # Fallback al prev
    direct = prev.get("system_prompt")
    if isinstance(direct, str):
        return direct
    return ""


def _extract_workspace(ctx: Any) -> str:
    """Lee cwd del ctx."""
    if hasattr(ctx, "workspace"):
        workspace = ctx.workspace
        if isinstance(workspace, str):
            return workspace
    raise ValueError(
        "agent_run stage requires ctx.workspace (directory where agent operates)"
    )


def _extract_max_turns(ctx: Any) -> int:
    """Lee max_turns del ctx — default 12 si no especificado."""
    if hasattr(ctx, "max_turns"):
        max_turns = ctx.max_turns
        if isinstance(max_turns, int) and max_turns > 0:
            return max_turns
    return 12  # default razonable, coincide con ResolveConfig default histórico

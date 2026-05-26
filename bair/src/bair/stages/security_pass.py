"""Security pass — mandatory hook stage que ejecuta scans y aplica gating.

Plan v7 sección 5. Sherlock Forensics 2026: 31% de vulnerabilidades
AI-introduced vienen de helpers/utils/config tweaks que scanners
tradicionales no detectan. Este stage convierte ese patrón en un hook
del Executor que NO puede skippearse cuando el diff toca categorías de
riesgo — corre, scanea, y gatekeepea.

## Wiring de policies/

Este es el primer stage que **enchufa** funciones de `policies/security.py`
a un Stage real:

- `needs_security_pass(signals, plan)` decide si el scan corre o se skip
- `gate_security(findings, plan)` decide si el resultado pasa o bloquea

Las policies son funciones puras testables sin LLM. El stage es la cápsula
ejecutiva que las invoca con state real.

## Por qué factory pattern

El scanner (placeholder ahora, bandit/semgrep/custom en F-future) es
inyectable. La factory `make_security_pass_stage(scanner)` produce un StageFn
con el scanner cerrado. Esto permite:

- Tests inyectan fake_scanner que devuelve findings programados
- Producción inyecta real_scanner que ejecuta semgrep/bandit
- Otros pipelines inyectan distintos scanners según el contexto (work usa
  scanner agresivo, review usa scanner más laxo)

Sin factory, el stage tendría que importar el scanner concreto o leerlo del
context — peor acoplamiento.

## Contrato del scanner

```python
ScannerFn = Callable[[DiffSignals], list[dict[str, Any]]]
```

Recibe los signals del diff (qué tipo de archivos cambiaron). Devuelve una
lista de findings, donde cada finding es un dict con al menos un campo
`severity` (`critical | high | medium | low | info`). El gating lee ese campo.

## Inputs del prev

El stage espera en `prev`:
- `plan`: ExecutionPlan (necesario para gate_security)
- `diff_signals`: DiffSignals (necesario para needs_security_pass)

Ambos deben ser provistos por stages upstream — típicamente un
`compute_diff_signals` que analiza el output de `gather_diff`. F-bridge v2
work añadirá ese stage. Mientras tanto, los tests construyen prev manualmente.
"""

from __future__ import annotations

from typing import Any, Callable

from ..domain.plan import Complexity, ExecutionPlan
from ..orchestration.stage import StageFn
from ..policies.security import (
    DiffSignals,
    SecurityGate,
    gate_security,
    needs_security_pass,
)


# Firma del scanner — recibe signals, devuelve lista de findings.
ScannerFn = Callable[[DiffSignals], list[dict[str, Any]]]


def make_security_pass_stage(scanner: ScannerFn) -> StageFn:
    """Construye un StageFn de security_pass con el scanner cerrado por closure.

    Args:
        scanner: callable que recibe DiffSignals y devuelve findings.
            En tests, un fake que retorna lista programada. En producción
            (F-future), wrapper sobre bandit/semgrep/custom.

    Returns:
        Función con firma StageFn lista para construir un Stage:

        ``Stage(name="security_pass", fn=make_security_pass_stage(scanner), ...)``
    """

    def security_pass(ctx: Any, prev: dict[str, Any]) -> dict[str, Any]:
        """Ejecuta el security pass — mandatory hook.

        Lee plan y diff_signals de `prev`. Si signals son "sin riesgo", skip.
        Si hay riesgo, corre el scanner, luego gate_security.
        """
        plan = _extract_plan(prev)
        signals = _extract_signals(prev)

        if not needs_security_pass(signals, plan):
            return {
                "skipped": True,
                "reason": "no risk categories touched",
                "findings_count": 0,
                "passed": True,
                "severity": "info",
            }

        findings = scanner(signals)
        gate: SecurityGate = gate_security(findings, plan)

        return {
            "skipped": False,
            "passed": gate.pass_,
            "severity": gate.severity,
            "reason": gate.reason,
            "findings_count": len(findings),
            "findings": findings,  # full payload for debugging
        }

    return security_pass


def _extract_plan(prev: dict[str, Any]) -> ExecutionPlan:
    """Extrae ExecutionPlan de prev. Fallback a MEDIUM si no lo encuentra.

    Razón del fallback: el security_pass NO debe fallar porque un stage upstream
    no produjo plan — eso debería ser un PipelineError de construcción, no
    runtime. El fallback documenta esta tolerancia y produce comportamiento
    razonable (MEDIUM complexity, sin flags especiales).
    """
    # Soporta dos shapes:
    # 1. prev["plan"] = ExecutionPlan directamente (stage upstream que produjo plan)
    # 2. prev["classify"]["plan"] = ExecutionPlan (stage classify típico)
    direct = prev.get("plan")
    if isinstance(direct, ExecutionPlan):
        return direct
    classify_output = prev.get("classify", {})
    if isinstance(classify_output, dict):
        nested = classify_output.get("plan")
        if isinstance(nested, ExecutionPlan):
            return nested
    return ExecutionPlan(complexity=Complexity.MEDIUM)


def _extract_signals(prev: dict[str, Any]) -> DiffSignals:
    """Extrae DiffSignals de prev. Fallback a vacío si no lo encuentra.

    Vacío significa "nada tocado en categorías de riesgo" → needs_security_pass
    devolverá False → stage skip. Sano default.

    Soporta tres shapes de upstream para tolerancia a distintos naming:
    - ``prev["diff_signals"]`` directo (caso minimalista)
    - ``prev["compute_diff_signals"]["signals"]`` (stage que computa del git diff)
    - ``prev["derive_diff_signals"]["signals"]`` (stage heurístico desde agent outcome)
    """
    direct = prev.get("diff_signals")
    if isinstance(direct, DiffSignals):
        return direct
    for stage_name in ("compute_diff_signals", "derive_diff_signals"):
        upstream = prev.get(stage_name, {})
        if isinstance(upstream, dict):
            nested = upstream.get("signals")
            if isinstance(nested, DiffSignals):
                return nested
    return DiffSignals()


# -- Placeholder scanner ----------------------------------------------------


def noop_scanner(signals: DiffSignals) -> list[dict[str, Any]]:
    """Scanner placeholder — siempre devuelve sin findings.

    Existe para que un pipeline pueda enchufar security_pass aunque todavía
    no haya un scanner real implementado. Resultado: el gate siempre pasa.

    F-future reemplaza con un wrapper sobre bandit/semgrep/custom.
    """
    return []

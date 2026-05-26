"""Stages — composable building blocks para Pipelines.

F3 deja este paquete intencionalmente vacío. La fundación arquitectónica
(orchestration/) está en lugar; production stages se extraerán de los
pipelines existentes en F4+:

- classify        — extraído del classifier actual
- gather          — extraído de gatherers/
- evidence_gate   — NUEVO (F5, legal safety layer)
- generate_codex  — extraído de work.py
- synthesize_claude — extraído del agent_sdk_provider usage
- claim_decomp    — NUEVO (F5, legal safety layer)
- security_pass   — NUEVO (F6, mandatory hook)
- publish         — extraído de publisher logic

En F3 este módulo solo declara el namespace y documenta el plan. Cualquier
stage agregado aquí debe:

1. Implementar la firma `StageFn` (`(context, prev) -> output`)
2. Tener tests en `tests/test_stages_*.py`
3. NO importar de `pipelines/` (eso es coupling inverso)
4. Tipar inputs externos contra `contracts/` Protocols, no contra
   implementaciones concretas de `infra/`
"""

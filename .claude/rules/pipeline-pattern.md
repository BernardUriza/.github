# Pipeline Pattern (bair consumer of xair)

Every bair pipeline follows the `gather → format → LLM → emit` shape. Pipelines self-register with `@command("name")` from `xair.command_registry` and live under `bair/src/bair/pipelines/`.

## The Canonical Shape

```python
# bair/src/bair/pipelines/<name>.py
from xair.command_registry import command, CommandContext, register_ack_meta
from xair.infra.container import Container

@command("<name>")
def <name>(ctx: CommandContext, container: Container) -> None:
    # 1. gather — read PR / diff / issue / commit context
    ctx_data = gather_something(container.github, ctx)

    # 2. format — build the prompt
    system_prompt = "..."
    user_msg = format_user_message(ctx_data)

    # 3. LLM — call the provider (Anthropic via httpx for gatekeep; container.llm for legacy)
    decision = call_anthropic(system_prompt, user_msg)

    # 4. emit — PR comment, $GITHUB_OUTPUT, exit code
    post_comment(container, ctx.repo, ctx.pr_num, render(decision))
    set_output("verdict", decision.verdict)
    if decision.blocks_merge:
        sys.exit(1)

register_ack_meta("<name>", icon="🛡️", label="<Name>")
```

## Side-Effect Registration

The `@command(...)` decorator only runs when the module loads. For `python -m bair <name>` to find the handler, the pipeline module MUST be imported at `bair` package load:

```python
# bair/src/bair/pipelines/__init__.py
from . import gatekeep  # noqa: F401  # pyright: ignore[reportUnusedImport]
from . import <new-pipeline>  # noqa: F401  # pyright: ignore[reportUnusedImport]
```

Forgetting this line is the #1 cause of "unknown command" errors after adding a new pipeline. Every code review of a new pipeline MUST grep for the `from . import` line in `__init__.py`.

## Why @command And Not Hardcoded Routing

xair's dispatch is registry-driven (see `xair/.claude/rules/registry-pattern.md`). It does NOT hardcode names. If bair routed its own commands instead of using `@command`, every new pipeline would need a dispatch.py edit. With the registry pattern, the only file touched per new pipeline is `pipelines/<name>.py` + one line in `pipelines/__init__.py`.

## The gather/format/LLM/emit Boundaries

| Phase | Where | Examples |
|-------|-------|----------|
| **gather** | `bair/gatherers/` or `xair/gatherers/` | diff, commits, issues, CI status |
| **format** | `bair/prompt/` | builders that produce `{system: str, user: str}` |
| **LLM** | direct httpx call OR `container.llm` | Anthropic Messages API, OpenAI Chat Completions |
| **emit** | inside the pipeline file | PR comment, $GITHUB_OUTPUT, log, file artifact |

Mixing phases (e.g., a "gatherer" that also formats a prompt) is a code smell — it usually means the gatherer is consumer-specific and shouldn't live in `xair/gatherers/`.

## What Already Exists Today

| Pipeline | Decorator | Status |
|----------|-----------|--------|
| `gatekeep` | ✅ `@command("gatekeep")` | LIVE on server-bot and free-intelligence |

The legacy pipelines (`changelog`, `review`, `retro`, `preflight`, `remedy`, …) were
deleted in #6/#7 — they were never registered. A new pipeline starts from the
canonical shape above; `gatekeep.review()` is the reference for keeping the verdict
path pure enough that an eval suite can call it (see `bair/src/bair/evals/`).

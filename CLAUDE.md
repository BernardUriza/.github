# BernardUriza/.github · Quick Reference

**Hosts Bernard's org-wide GitHub artifacts:** the `bair` thin consumer of [xair](https://github.com/BernardUriza/xair), reusable GitHub Actions workflows that consumer repos call via `workflow_call`, and the BAIR Gatekeeper Brython + ApexCharts dashboard.

**Owner:** Bernard Uriza Orozco
**Repo:** https://github.com/BernardUriza/.github
**Active in production:** BAIR Gatekeeper pipeline running on [BernardUriza/free-intelligence](https://github.com/BernardUriza/free-intelligence) since 2026-05-27.

---

## 🚀 Quick Start

```bash
# Install bair locally (depends on xair from git)
cd bair
pip install -e .

# Run a pipeline manually (gatekeep is the only registered command today)
python -m bair gatekeep
```

`bair` reads its trigger options from env (`PR_NUM`, `REPO`, `BASE_SHA`, `HEAD_SHA`, `ANTHROPIC_API_KEY`, `GH_TOKEN`) — the GitHub Actions workflow in the consumer repo sets these and calls `python -m bair <cmd>`.

---

## 📚 Layout

```
.github/workflows/         # reusable workflows callable from any repo
└── ai-gatekeep.yml        # workflow_call entry for BAIR Gatekeeper
                           # (TEMPORARILY UNUSED — consumers inline the same
                           # steps because cross-repo workflow_call trips
                           # startup_failure; see free-intelligence pr-gate.yml)

bair/                      # the BAIR consumer of the X-AIR pattern
├── pyproject.toml
├── src/bair/
│   ├── __init__.py
│   ├── __main__.py        # python -m bair <cmd> → xair.dispatch
│   ├── pipelines/
│   │   ├── __init__.py    # side-effect imports → @command registrations
│   │   ├── gatekeep.py    # the LIVE gatekeeper (BLOCK/WARN/APPROVE)
│   │   ├── changelog.py   # legacy (not @command-decorated yet)
│   │   ├── review.py      # legacy
│   │   ├── retro.py       # legacy
│   │   └── ...
│   ├── prompt/            # pipeline-specific prompt formatters
│   ├── config/            # per-pipeline frozen dataclasses
│   ├── domain/            # consumer-specific models (CSS analyzer, etc.)
│   ├── gatherers/         # consumer-specific gatherers (deep_analysis, etc.)
│   ├── stages/            # composable Pipeline stages
│   ├── tools/             # backfill / snapshot / validate utilities
│   ├── services/          # changelog deliverers, local_runner
│   └── ...
└── frontend/              # Brython + ApexCharts observability dashboard
    ├── index.html
    ├── observability.html
    ├── css/, img/, js/
    ├── py/                # Brython app modules (run IN THE BROWSER)
    └── diagrams/          # static HTML pipeline diagrams
```

---

## 🎯 Core Principles

1. **bair extends xair via the registry pattern.** Pipelines self-register with `@command("name")`; the dispatcher in xair routes by name. → [rules/pipeline-pattern.md](.claude/rules/pipeline-pattern.md)
2. **Frontend is Brython, not JavaScript.** Logic lives under `frontend/py/`; the `<script type="text/python">` tag runs it in the browser. → [rules/frontend-brython.md](.claude/rules/frontend-brython.md)
3. **No secrets in this repo.** Secrets (`BAIR_APP_ID`, `BAIR_APP_PRIVATE_KEY`, `ANTHROPIC_API_KEY`) live in CONSUMER repos' Actions secrets, never in code or workflow YAML literals.

---

## 🏗️ Adding a Pipeline

1. Write `bair/src/bair/pipelines/<name>.py` with handler decorated `@command("<name>")` from `xair.command_registry`
2. Add `from . import <name>  # noqa: F401  # pyright: ignore[reportUnusedImport]` to `bair/src/bair/pipelines/__init__.py`
3. Wire from a CONSUMER repo's workflow YAML — call `python -m bair <name>` with required env (`PR_NUM`, `REPO`, `BASE_SHA`, `HEAD_SHA`, plus pipeline-specific keys)
4. Commit + push. No PyPI publish needed; consumer pip-installs from `git+https://github.com/BernardUriza/.github@main#subdirectory=bair`.

---

## 🚫 Critical Rules

### bair imports from xair, NOT the other way around

```python
# ✅ Pipeline imports the framework
from xair.command_registry import command, CommandContext
from xair.infra.container import Container
```

```python
# ❌ Don't make xair depend on bair (would force circular)
# xair MUST stay framework-only — see xair/.claude/rules/framework-genericity.md
```

### Side-effect imports in `pipelines/__init__.py` are mandatory

Without them, `@command("name")` decorators never run, the registry stays empty, `python -m bair <cmd>` returns "unknown command". Every new pipeline MUST add one line to `__init__.py`.

### Frontend is Brython

```html
<!-- ✅ Browser runs the Python directly -->
<script type="text/python" src="py/app.py"></script>
```

Do NOT translate Brython to JS to "modernize". The frontend was deliberately kept in Python so the entire BAIR stack speaks one language.

### Cross-repo reusable workflow currently broken

`workflow_call` from another repo to `BernardUriza/.github/.github/workflows/ai-gatekeep.yml@main` trips `startup_failure` for reasons not fully diagnosed (see free-intelligence pr-gate.yml history, commits `42f30778` + `5de1c2b5`). Until resolved, **inline the workflow steps** in each consumer's repo instead of `uses:` the reusable. ~30 LOC of duplication is an acceptable trade vs the cross-repo opacity.

---

## 🔗 Related

- [xair](https://github.com/BernardUriza/xair) — the framework bair depends on
- [free-intelligence](https://github.com/BernardUriza/free-intelligence) — first repo running the BAIR Gatekeeper via App-token integration
- BAIR GitHub App: https://github.com/apps/bair-gatekeeper (id `3878034`, installation `135930809` on free-intelligence)
- BAIR logo: morado neural network — designed in Gemini, lives on the App page

---

## 🏷️ Conventions

- **Commits:** Conventional Commits (`feat/fix/docs/chore/test/refactor`)
- **Branches:** push directly to `main`. No PR workflow set up here yet (this repo doesn't run its own CI).
- **Language:** English in `.claude/rules/*.md` per the cross-project rule.

---

For deeper documentation, browse [`.claude/rules/`](.claude/rules/).

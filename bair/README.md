# bair — Bernard's AI Reviewer

Thin consumer of [xair](https://github.com/BernardUriza/xair). Wires the
generic X-AIR framework against Bernard's ecosystem (free-intelligence,
fi-core, fi-runner, insult, alice, ferboli, aurity, claude-code-notifications,
bernard-blog).

## Layout

```
bair/
├── pyproject.toml      # depends on xair @ git+…
├── src/bair/
│   ├── __main__.py     # `python -m bair <cmd>` → xair.dispatch
│   ├── pipelines/      # Bernard-specific commands (extend xair base)
│   ├── prompts/        # tone/context overrides
│   └── gatherers/      # readers for FI-specific data sources
└── frontend/           # Brython + ApexCharts dashboard (inherited from VAIR pattern)
```

## Usage

The repo `BernardUriza/.github` hosts reusable GitHub Actions workflows in
`.github/workflows/ai-*.yml` that call `python -m bair <command>` against a
target repo's checkout. Target repos add a 12-line passthrough at
`.github/workflows/ai-commands.yml` with `uses:` + `secrets: inherit`.

See the parent repo `README.md` for the per-target wiring template.

## Local install

```bash
pip install -e bair/
python -m bair --help
```

Pulls xair from GitHub main; expect the lock to evolve until xair tags a
release.

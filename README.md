# BernardUriza/.github

Home of **bair**, the BAIR Gatekeeper: an AI pull-request gate that reviews every PR of a consumer repo with Claude and returns APPROVE / WARN / BLOCK (only BLOCK fails the check).

## What lives here

```
bair/                 the gatekeeper (thin consumer of xair)
├── src/bair/         pipelines/gatekeep.py, gatherers/, prompts/, evals/
└── tests/
.github/workflows/    smoke-test-anaconda-consumption.yml (fi-core / fi-runner / xair)
.claude/backlog/      the gate's calibration log
```

## How a repo uses the gate

Copy [server-bot's `ai-gatekeep.yml`](https://github.com/BernardUriza/server-bot/blob/main/.github/workflows/ai-gatekeep.yml) into the consumer repo, add the secrets `BAIR_APP_ID`, `BAIR_APP_PRIVATE_KEY` and `CLAUDE_CODE_OAUTH_TOKEN`, and install the [BAIR GitHub App](https://github.com/apps/bair-gatekeeper). The workflow installs bair from `main` and runs `python -m bair gatekeep`; the verdict lands as a PR comment from `bair-gatekeeper[bot]`.

(There is no reusable `workflow_call` here: calling it cross-repo trips `startup_failure`, so each consumer inlines the ~30 lines.)

## How the gate is tuned

By measurement, not by feel: `python -m bair.evals` replays labelled commits from a consumer's history (real regressions found with SZZ, plus clean commits) through the same code path the live gate uses, and reports catch rate, false BLOCKs and verdict flips with Wilson intervals. See `CLAUDE.md` and `.claude/backlog/01-the-gate-has-never-said-no.md`.

## Relationship to xair

[xair](https://github.com/BernardUriza/xair) is the generic framework (command registry, container, GitHub client). `bair` is Bernard's instance of it.

## License

MIT.

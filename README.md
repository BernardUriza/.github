# BernardUriza/.github

Org-level shared infrastructure for Bernard's repositories.

## What lives here

```
.github/workflows/   reusable GitHub Actions workflows (workflow_call)
                     called as: uses: BernardUriza/.github/.github/workflows/<name>.yml@main
bair/                thin consumer of xair, wired against Bernard's ecosystem
                     ├── frontend/   dashboard (Brython + ApexCharts)
                     └── src/bair/   pipelines, prompts, gatherers
```

## How target repos use it

A target repo (e.g. `free-intelligence`, `insult`, `alice`) drops a
`.github/workflows/ai-commands.yml` passthrough:

```yaml
name: AI Commands
on:
  pull_request_review_comment:
    types: [created]
  issue_comment:
    types: [created]
  workflow_dispatch:

jobs:
  ai-review:
    if: contains(github.event.comment.body, '/ai-review')
    uses: BernardUriza/.github/.github/workflows/ai-review.yml@main
    secrets: inherit
```

All logic (model selection, prompts, secret names, channels) lives here.
Updating a prompt or swapping a model means one push to this repo; no
fan-out PRs across N target repos.

## Relationship to xair

[xair](https://github.com/BernardUriza/xair) is the generic OSS framework
(pip-installable). `bair/` here is the **instance** — Bernard's
configuration of xair, with custom pipelines, prompts, gatherers, and the
frontend dashboard.

## License

MIT.

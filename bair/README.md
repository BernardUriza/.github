# bair — the BAIR Gatekeeper

Thin consumer of [xair](https://github.com/BernardUriza/xair): one command, `gatekeep`, that reviews a pull request with Claude and gates the merge, plus the eval suite that decides how it is tuned.

## Layout

```
bair/
├── pyproject.toml            depends on xair @ git+…
├── src/bair/
│   ├── __main__.py           python -m bair <cmd> → xair.dispatch
│   ├── pipelines/gatekeep.py @command("gatekeep") + review()
│   ├── gatherers/            repo_rules.py, changed_context.py
│   ├── prompts/              gatekeep_system.md
│   └── evals/                python -m bair.evals (cases, runner, stats)
└── tests/                    offline, no LLM calls
```

## Usage

```bash
pip install -e ".[test]"
python -m pytest

# the gate (what a consumer's workflow runs)
REPO=owner/repo PR_NUM=1 BASE_SHA=… HEAD_SHA=… python -m bair gatekeep

# the eval suite, from a full-history checkout of the cases' repo
python -m bair.evals --repo-dir ../server-bot --runs 3 --context slice --models claude-opus-4-7
```

Knobs: `BAIR_GATEKEEP_MODEL` (Claude model), `BAIR_CONTEXT` (`slice` default, `full`, `none`).

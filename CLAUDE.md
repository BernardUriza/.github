# BernardUriza/.github · Quick Reference

**Hosts `bair`**, the BAIR Gatekeeper: a thin consumer of [xair](https://github.com/BernardUriza/xair) that reviews every PR of a consumer repo with Claude and returns APPROVE / WARN / BLOCK. Only BLOCK fails the check.

**Owner:** Bernard Uriza Orozco
**Repo:** https://github.com/BernardUriza/.github (PUBLIC)
**Live on:** [server-bot](https://github.com/BernardUriza/server-bot) (`.github/workflows/ai-gatekeep.yml`) and [free-intelligence](https://github.com/BernardUriza/free-intelligence) (`pr-gate.yml`). Consumers `pip install` bair from `main`, so **a push to `main` here is a deploy to every consumer's next PR.**

---

## 🚀 Quick Start

```bash
cd bair && pip install -e ".[test]"   # pulls xair from git main
python -m pytest                      # offline, no LLM calls
python -m bair gatekeep               # needs REPO, PR_NUM, BASE_SHA, HEAD_SHA + a Claude credential
```

Credentials, in the order the gate tries them: `CLAUDE_CODE_OAUTH_TOKEN` (Max pool, preferred) → `ANTHROPIC_API_KEY` → `OPENAI_API_KEY`. None answering → the gate **abstains out loud** (comment + exit 0), never blocks on infrastructure.

---

## 📚 Layout

```
bair/src/bair/
├── __main__.py                python -m bair <cmd> → xair.dispatch
├── pipelines/
│   ├── __init__.py            side-effect imports → @command registrations
│   └── gatekeep.py            @command("gatekeep") + review(): the shared verdict path
├── gatherers/
│   ├── repo_rules.py          target repo's .claude rules + curated playbook rules
│   └── changed_context.py     code around the diff (BAIR_CONTEXT = slice | full | none)
├── prompts/
│   └── gatekeep_system.md     the system prompt (content, not code)
└── evals/                     python -m bair.evals — the eval suite
    ├── cases_server_bot.json  labelled (base, head) cases from server-bot history
    ├── __main__.py            runner: worktree per case, JSONL rows, report
    └── stats.py               majority verdict, Wilson intervals, discordance
.claude/backlog/               01 = the gate's calibration log (receipts, decisions)
```

## 🛡️ How a verdict is made (`gatekeep.review`)

1. **Context:** repo rules + playbook rules + `changed_context` (default `slice`: function bodies along the data flow — changed functions, callers, same-module readers of the hunk's data keys, one hop into helpers).
2. **LLM:** `_call_llm` → Claude (default `claude-opus-4-7`, override `BAIR_GATEKEEP_MODEL`) → `_normalize` (validated verdict/severity/issues).
3. **Floor:** the verdict never sits below its worst issue (CRITICAL → BLOCK, HIGH → ≥ WARN).
4. **Shadow:** a BLOCK resting only on issues with `rule: data_plane` exits as WARN with `would_block=true`. Promotion = emptying `_SHADOW_RULES`, only on eval evidence.
5. **Exit:** 1 only on BLOCK.

The live gate and the eval suite call the **same** `review()` — change it and the suite measures exactly what ships.

## 🧪 Changing the gate = measuring it

Any change to the prompt, the model, the context or the verdict logic is decided by the eval suite, never by one PR:

- **Where it runs:** server-bot branch `bair-eval`, workflow `.github/workflows/bair-eval.yml` (push to that branch triggers it; nothing touches main/CI/CD). Edit `EVAL_ARGS` there, push an empty commit, read the job summary + the `bair-eval-jsonl` artifact.
- **Protocol:** fix the decision rule BEFORE the run; decide on **held-out** only; dev may be read case by case, held-out only in aggregate (a held-out case you read moves to dev and gets replaced).
- **Labels:** defects come from SZZ (blame the lines each `fix` commit changed) verified by hand; a "clean" case the gate flags gets audited — a real bug becomes a *latent* defect, not a false positive.
- Full log, numbers and open decisions: `.claude/backlog/01-the-gate-has-never-said-no.md`.

---

## 🚫 Critical Rules

- **bair imports xair, never the reverse.** xair stays framework-only.
- **Every `@command` needs its side-effect import** in `pipelines/__init__.py`, and the decorator must sit directly on the handler — `tests/test_gatekeep_registration.py` pins `get_handler("gatekeep") is gatekeep.gatekeep` (a helper once got inserted between them and every consumer's gate crashed).
- **No secrets here.** They live in consumer repos' Actions secrets. See `.claude/rules/secrets-handling.md`.
- **Consumers inline the workflow.** Cross-repo `workflow_call` trips `startup_failure`, so there is no reusable workflow here; the canonical template is server-bot's `.github/workflows/ai-gatekeep.yml` (OAuth + App token + `python -m bair gatekeep`).

## 📜 Constitution

Governed by agent-constitution, **profile `personal`** (only Bernard works here; that consumers `pip install` it from `main` raises the stakes, not the ownership). Policy `repo > personal > core`: this file and `.claude/rules/*.md` stay the nearest authority. Committed: `.claude/constitution.{toml,lock,toolchain.toml}` + `.claude/.gitignore`; everything `constitution install` materializes (`.claude/rules/_constitution/`, the store, hooks in `settings.local.json`, linked capabilities) is gitignored. `bair`'s `repo_rules` deliberately skips `_constitution/` so a PR gets the same doctrine on a dev machine as in CI. Re-verify with `constitution verify` (must be CLEAN).

## 🏷️ Conventions

- **Commits:** Conventional Commits. **Branches:** push directly to `main` (no CI here — run `pytest` before pushing, it IS the deploy).
- **Language:** English in `.claude/rules/*.md`.

## 🔗 Related

- [xair](https://github.com/BernardUriza/xair) — the framework
- BAIR GitHub App: https://github.com/apps/bair-gatekeeper (id `3878034`)

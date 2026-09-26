# 03 — The reviewer reads the whole repo (read-only repo MCP through AIRE)

Status: **In progress**
Proposed: 2026-09-25 by Bernard ("hagamos a bair más crítico y con más poderes… clone permanente y así siempre abre el revisor adversario en aire ya clonado"; "dale, todo en solo lectura")

## What it is

The gate stops seeing only the diff + slice: an agentic AIRE turn with read-only
tools over the whole repository at the PR's base and head.

- Builds/tests on AIRE were rejected: PR code is untrusted, and the droplet holds
  the model credential, runs as root, 512 MB, serves the personas live.
- A clone INSIDE AIRE was rejected by aire-server's decision #3 ("AIRE doesn't
  touch git; git enters as a tool configured from outside"), and server-bot is
  private (a GitHub credential on the droplet). Bernard picked option A.
- Canonical path reused: aire-server #48 `remote_tools` (caller-hosted HTTP MCP,
  same shape as persona-runner's `/mcp/{casita}` and og118's background door).

## Built (uncommitted in the working tree as of 2026-09-25 — see next step)

- `bair/src/bair/repo_mcp/` — permanent bare mirror per repo; `POST /prepare`
  (bearer + the runner's GitHub token → ls-remote proof, fetch base/head, capsule
  pinned to repo+SHAs, `REPO_MCP_REPOS` allowlist) and `POST /mcp/{capsule}`
  (list_files, read_file, grep, git_log, blame, diff). Nothing writes or executes.
- `gatekeep.review(..., base_sha, head_sha)`; `BAIR_REPO_TOOLS=on` +
  `BAIR_REPO_MCP_URL` + `BAIR_REPO_MCP_TOKEN` mount it in mode `complete` (zero
  builtins: no Write, no WebFetch). Off by default. Prompt addendum:
  `prompts/gatekeep_repo_tools.md`. Provider shows `aire+repo`.
- `bair/Dockerfile.repo-mcp`, `.github/workflows/repo-mcp.yml` (GHCR → Container
  App `bair-repo-mcp`, insult-rg/prod-env, verifies /health 200 + unauth 404).
- Tests: 78 green (7 MCP against a real local origin, 3 wiring).
- Side PR: aire-server #2 (backlog #37, `builtins` subset) — open, NOT needed by
  this design (bair uses mode complete), not merged.

## The decision that's the owner's

1. Permission to create Azure resources: the Container App, a service principal
   scoped to it (`AZURE_CREDENTIALS` in this repo) — the harness denied the create
   AND the local commit as "Production Deploy".
2. True permanence of the mirror: `min-replicas 0` (free grant, mirror re-cloned
   after scale-to-zero) vs `min-replicas 1` (a few USD/month, mirror stays warm).

## Next step

Commit + push; create `bair-repo-mcp`; SP → `AZURE_CREDENTIALS`; add its origin to
`AIRE_REMOTE_TOOL_ORIGINS` (`~/.secrets/aire-remote-tools.txt` + canonical deploy);
`BAIR_REPO_MCP_URL/TOKEN` into server-bot's secrets; A/B on the `bair-eval` branch
(`BAIR_REPO_TOOLS` off vs on) against the baseline of run 36218765788, decision
rule fixed before the run. Promote to the live gate only on held-out evidence.

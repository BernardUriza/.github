# Secrets Handling

This repo hosts code and workflow YAML. It hosts ZERO secret values.

## The Hard Rule

NEVER commit any of these strings to this repo:

| Pattern | Type |
|---------|------|
| `sk-ant-…` | Anthropic API key |
| `sk-…`, `sk-proj-…` | OpenAI key |
| `ghp_…`, `github_pat_…` | GitHub PAT |
| `gho_…` | GitHub OAuth token |
| `xoxb-…`, `xoxp-…` | Slack token |
| BEGIN RSA / EC / OPENSSH / PRIVATE KEY blocks | Any private key |
| Base64 JWT (`eyJ…`) | Any signed token |
| Anything Bernard calls a "secret", "token", "key", "password", "credential" | Generic |

If you have to handle one, the right destination is:

1. **GitHub Actions secret** in the CONSUMER repo (e.g., `gh secret set BAIR_APP_PRIVATE_KEY --repo BernardUriza/free-intelligence`)
2. **`~/.secrets/<service>-<purpose>.txt`** for local-dev usage. NEVER inside any git repo.
3. **NEVER** in `.claude/`, `.github/`, `bair/`, `frontend/`, or any committed dir.

## Why It Matters

This repo is PUBLIC. Anyone can clone it. If a secret lands in a commit, even if you `git rm` and force-push later:

- GitHub's PII detection MAY flag and revoke the secret (Anthropic, OpenAI, GitHub PATs all subscribe to GitHub's secret-scanning alerts)
- The secret remains in the git history of any clone someone made between push and revoke
- Cleanup requires force-push + asking GitHub to purge cached versions

The damage outpaces the convenience. There is no scenario where committing a secret to this repo is the right move.

## The Live BAIR Setup (no secrets here)

The BAIR Gatekeeper auth chain works WITHOUT any secret living in this repo:

1. **Consumer repo** (e.g., `BernardUriza/free-intelligence`) has Actions secrets:
   - `BAIR_APP_ID` = `3878034`
   - `BAIR_APP_PRIVATE_KEY` = contents of the .pem from GitHub App creation
   - `ANTHROPIC_API_KEY` = the Claude API key
2. **Consumer workflow** (`pr-gate.yml`) calls `actions/create-github-app-token@v1` with those secrets → outputs a short-lived (~1h) installation token
3. **Consumer workflow** runs `python -m bair gatekeep` with `GH_TOKEN: ${{ steps.app-token.outputs.token }}` + `ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}`
4. **bair package** (this repo) reads those env vars at runtime. It never knows the static secret values — it gets ephemeral env values from the consumer's workflow.

That is how an open-source bot stays secret-free: the secrets live where the consumer controls them, the bot just consumes env vars.

## If You Discover a Secret in a Commit

```bash
# 1. Revoke the secret IMMEDIATELY at the source (GitHub Settings → Secrets, Anthropic Console, etc.)
# 2. Force-push removal (acceptable risk for a leaked credential):
git rm <file-with-secret>
git commit -m "fix: remove leaked secret (revoked)"
git push --force-with-lease

# 3. Ask GitHub to purge cached versions:
gh api -X POST /repos/BernardUriza/.github/actions/caches/purge

# 4. Re-issue the secret at the source. Update consumer repos' Actions secrets.
# 5. Run secret scanning to confirm no other leaks:
gh secret-scanning alerts list --repo BernardUriza/.github
```

`--force-with-lease` is one of the few cases where force-push is justified — a public leak costs more than rewrite history pain.

## Anti-Patterns To Refuse

- "Just commit the key for now, we'll rotate later" — NO. Rotate first, then code without it.
- "It's in a private gist that's linked in the README" — equivalent to public; gists are searchable.
- "Encrypted in the repo with a passphrase in the README" — defeats the purpose.
- "Only in CI logs, not in source" — `echo $SECRET` in a workflow makes it public in the run log unless `::add-mask::` is used; never echo a secret without masking.

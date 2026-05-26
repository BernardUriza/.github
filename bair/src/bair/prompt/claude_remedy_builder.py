"""Build the rendered prompt for /ai-remedy via Claude Code Action.

Mirrors prompt/claude_builder.py but loads `claude-remedy.md` and injects
a `guidance` field so operator hints from the comment body propagate to
Claude. The remedy contract itself lives in `ai-rules/commands/remedy-mr.md`,
which the prompt instructs Claude to follow verbatim.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..infra.constants import TEMPLATES_DIR
from ..log import logger

_PROMPT_DIR = TEMPLATES_DIR.parent
_RULES_DIR = Path("ai-rules/rules/shared")
_LEARNINGS_DIR = Path("learnings")
_OUTPUT_FILE = Path("/tmp/claude-prompt.md")


@lru_cache(maxsize=1)
def _load_template() -> str:
    path = _PROMPT_DIR / "claude-remedy.md"
    if not path.exists():
        raise FileNotFoundError(f"Claude remedy template not found: {path}")
    return path.read_text(encoding="utf-8")


def _load_rules() -> str:
    if not _RULES_DIR.exists():
        return ""
    parts = ["## Engineering Rules (from team standards)\n"]
    for rule_file in sorted(_RULES_DIR.glob("*.md")):
        content = rule_file.read_text(encoding="utf-8").strip()
        if content:
            parts.append(content)
            parts.append("")
    return "\n".join(parts) if len(parts) > 1 else ""


def _load_learnings(variant: str) -> str:
    learnings_file = _LEARNINGS_DIR / f"{variant}.yml"
    if not learnings_file.exists():
        return ""
    content = learnings_file.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    return f"## Validated Policies (from past reviews)\n\n```yaml\n{content}\n```"


_BODY_FORMATTING_NOTE = """**CRITICAL — body formatting:** ALWAYS write the review body to a file first, then pass it via `-F body=@/tmp/review-body.md`. NEVER inline the body with `-f body="..."` — the `-f` flag sends strings literally, so any `\\n` escape sequences end up rendered as the two characters `\\n` in the GitHub UI instead of real newlines, and the whole review collapses into one ugly line. The `Write` tool produces a real-newline file; `-F body=@<path>` reads it as a binary blob and preserves them. This is non-negotiable — every prior remedy that broke formatting violated this rule.
"""

_APPROVE_BLOCK_ON = """**APPROVE MODE: ON.** The user invoked `/ai-remedy approve`. After applying the remedy and verifying tsc/tests/threads, your final action MUST be an APPROVE review (or, if you cannot approve cleanly, a COMMENT review explaining why and STOP). Submit via:

**Mergeability precondition — non-negotiable.** Before submitting APPROVE, run BOTH:

```bash
gh pr view {pr_number} --json mergeStateStatus,mergeable --jq '{{mss: .mergeStateStatus, m: .mergeable}}'
gh pr checks {pr_number} --repo {repo} --json name,state,conclusion
```

The check rollup is mandatory — `mergeStateStatus` alone hides test/build failures behind the `UNSTABLE` label when those checks are not marked required by branch protection. "Not required" never means "ignorable" — a failing test suite is a failing test suite.

**Step 1: branch state.**
- `CLEAN` → continue to Step 2.
- `BEHIND` → run `git fetch origin && git merge origin/<base>` on the PR branch, resolve conflicts, run tsc + tests locally, force-push with `--force-with-lease`, then re-check. Only proceed to Step 2 when it becomes `CLEAN`.
- `DIRTY` or `mergeable=CONFLICTING` → attempt the same merge flow. If conflicts need teammate domain knowledge or resolution changes semantics, STOP and post `COMMENT` naming the conflicting files. **Do NOT APPROVE a DIRTY PR.**
- `UNKNOWN` → wait 10s and re-check; treat as DIRTY after 30s.
- `BLOCKED` → check the missing requirement. If a required check is still running, wait and retry; otherwise COMMENT and STOP.
- `UNSTABLE` → continue to Step 2 (the failing checks are non-required, but they still need inspection).
- `HAS_HOOKS` → continue to Step 2.

**Step 2: check rollup — every failing check must be triaged before APPROVE.**

For every entry in `gh pr checks` output where `conclusion` is anything other than `success`, `skipped`, or `neutral` (i.e. `failure`, `cancelled`, `timed_out`, `action_required`, or `state=in_progress`):

1. **Test, build, lint, or type-check fails (any of these in the name: `test`, `build`, `tsc`, `lint`, `eslint`, `jest`, `cypress`, `e2e`, `compile`, `Deploy`)** → BLOCKING. Do NOT APPROVE. Post `COMMENT` review naming each failing check and STOP. Required or not, a failing test is a failing test. The rule `test-failures-p0.md` says "every test failure is P0" — that applies here verbatim.
2. **Still running (`state=in_progress` or `pending`)** → wait up to 5 minutes for completion, then re-check. If still running after 5 min, post `COMMENT` saying "checks in flight" and STOP — do not APPROVE on faith.
3. **Genuinely irrelevant non-test check failing** (e.g. a Slack notification that 404'd, a pages-deploy fork, an external bot timeout) → APPROVE is allowed ONLY IF the review body explicitly names each failing check, why it's safe to ignore, and confirms no test/build/lint check is in that list. The reviewer reading the PR must see the disclosure — a silent APPROVE over a red X is a betrayal of the merge gate.

**APPROVE means "click merge now, the merged code is green by construction." Anything less is a COMMENT.** A previous run of this prompt treated `UNSTABLE` as a free pass and approved a PR while `Deploy Backend V2 Service / Test Backend Service` was failing for 14 minutes — Step 2 exists to make that bug impossible to repeat.

""" + _BODY_FORMATTING_NOTE + """
1. Use the `Write` tool to create `/tmp/review-body.md` with the full markdown body, ending in the hidden marker line:

   ```
   <!-- ai-remedy:approved:HEAD_SHA -->
   ```

2. Then submit:

```bash
gh api repos/{repo}/pulls/{pr_number}/reviews \\
  --method POST \\
  -f event=APPROVE \\
  -F body=@/tmp/review-body.md
```

Replace `HEAD_SHA` with the actual current PR head SHA (`gh pr view {pr_number} --json headRefOid --jq .headRefOid`). The hidden HTML-comment marker lets future runs detect that approval was already submitted at this SHA.

This identity comes from the BAIR org-level GitHub App, not the human author — that is intentional and is what makes the approval count toward the merge gate.

**DO NOT wait for in-flight CI inside this run.** This job is a single GitHub Actions process — it has no persistence beyond its own exit. If you push a fix and CI is still running, you cannot return to this session to approve once CI completes. Tools like `ScheduleWakeup`, `CronCreate`, or any "wait and re-check later" pattern will silently fail because the runtime is gone the moment this job exits.

When you push a remedy commit that triggers fresh CI, do this instead:
1. Push the commit.
2. Post a `COMMENT` review naming the fix and explicitly stating: "CI run <run-url> triggered by remedy commit <sha>; re-invoke `/ai-remedy approve` once it completes to get the final APPROVE."
3. STOP. The user re-runs the command after CI finishes; the next remedy invocation gets the green checks and approves cleanly.

The only acceptable wait pattern inside a single run is a synchronous `gh run watch <id> --exit-status` IF the CI is expected to finish in under ~10 minutes AND your job's timeout-minutes budget allows it. For anything longer, push + COMMENT + STOP.
"""

_APPROVE_BLOCK_OFF = """**APPROVE MODE: OFF.** The user invoked `/ai-remedy` WITHOUT the `approve` subcommand. After applying the remedy and verifying tsc/tests/threads, your final action MUST be a COMMENT review (NOT APPROVE) summarising what was changed. Submit via:

""" + _BODY_FORMATTING_NOTE + """
1. Use the `Write` tool to create `/tmp/review-body.md` with the full markdown body, ending in the hidden marker line:

   ```
   <!-- ai-remedy:STATUS:HEAD_SHA -->
   ```

2. Then submit:

```bash
gh api repos/{repo}/pulls/{pr_number}/reviews \\
  --method POST \\
  -f event=COMMENT \\
  -F body=@/tmp/review-body.md
```

Replace `STATUS` with `clean` if no code changes were needed (the PR was already in good shape) OR `applied` if you pushed remedy commits. Replace `HEAD_SHA` with the current PR head SHA AFTER any push you made (`gh pr view {pr_number} --json headRefOid --jq .headRefOid` — re-run after your push if applicable). The hidden HTML-comment marker is mandatory: it lets a follow-up `/ai-remedy approve` skip the entire Claude SDK invocation when the prior remedy on the same SHA was clean.

Do **NOT** call APPROVE under any circumstance in this run, even if the PR is fully clean. The user will invoke `/ai-remedy approve` separately when they want the approval.
"""


def build_claude_remedy_prompt(
    repo: str,
    pr_number: str,
    variant: str,
    guidance: str = "",
    approve_mode: bool = False,
) -> str:
    """Render the full Claude Code remedy prompt with learnings + rules + guidance.

    approve_mode controls whether the final action is an APPROVE review or a
    COMMENT review. /ai-remedy alone leaves it OFF; /ai-remedy approve sets it ON.
    """
    template = _load_template()
    learnings = _load_learnings(variant)
    rules = _load_rules()
    approve_block = (_APPROVE_BLOCK_ON if approve_mode else _APPROVE_BLOCK_OFF).format(
        repo=repo, pr_number=pr_number,
    )

    return template.format(
        repo=repo,
        pr_number=pr_number,
        variant=variant,
        learnings_section=learnings,
        rules_section=rules,
        guidance=guidance or "(none)",
        approve_section=approve_block,
    )


def write_claude_remedy_prompt_file(
    repo: str,
    pr_number: str,
    variant: str,
    guidance: str = "",
    approve_mode: bool = False,
) -> Path:
    """Build the prompt and write to /tmp/claude-prompt.md for YAML to consume."""
    prompt = build_claude_remedy_prompt(repo, pr_number, variant, guidance, approve_mode)
    _OUTPUT_FILE.write_text(prompt, encoding="utf-8")
    logger.info(f"Claude remedy prompt written to {_OUTPUT_FILE} ({len(prompt)} chars)")
    return _OUTPUT_FILE

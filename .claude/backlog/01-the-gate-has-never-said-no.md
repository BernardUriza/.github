# 01 — The gate has never said no: prove BAIR can fail, then tune it

Status: **In progress** 2026-09-25 — step 1 partly done (see Receipts) (from a review of server-bot PRs #93/#94/#95).

## The receipt

Last 40 PRs of `BernardUriza/server-bot` (tallied 2026-09-25 from the
`bair-gatekeeper` comments): **35 APPROVED, every one at severity LOW**, 2
UNAVAILABLE, 1 ABSTAINED. Zero WARN, zero BLOCK, zero MEDIUM. With that record
there is no way to tell "the PRs were good" from "a check that cannot fail" — and
a check that cannot fail proves nothing (Art. 2).

## What it does well (keep it)

- Reads the whole diff (250–300 lines per PR) and cites the repo's own rules.
- On #93 it named the two real risks: the audit ran on osx-arm64 while prod is
  linux-64, and starlette jumped a major version (0.x → 1.x). It asked for the
  post-deploy #general probe — exactly the right ask.

## What it missed on the same day

- **#94** ("Ship it"): the PR's own author reported a hole — if the Postgres update
  that marks a job as sent to AIRE fails, a resume folds the history twice — and
  BAIR did not surface it. Nor did it say that path is only verifiable by
  restarting the runner mid-generation.
- **#95**: summary says "21 dead ignores"; the diff removes 20. Silent on a merge
  commit made with `--no-verify` that skipped the version hook.
- **Cross-PR blindness**: each PR is reviewed alone. #93 and #95 collide in
  `ci.yml`/`environment.yml`, and nobody has audited the two combined; two open
  PRs (#94, #97) ship the same version tag `v4.40.23`. BAIR cannot see any of it.

## The work, in order

1. **Red test first.** Open a throwaway PR in server-bot with a known, serious
   defect — e.g. revert the #92 fix so a job rebuilt from its `turn_jobs` row
   drops its images again — and record the verdict. If it comes back
   APPROVED/LOW, the gate is decorative and the next steps are mandatory. Keep a
   small set of these planted-defect PRs as a regression suite for the prompt.
2. **Severity calibration** in `bair/src/bair/prompts/gatekeep_system.md`: today
   APPROVE covers "no issues OR LOW/MEDIUM", and in practice everything lands LOW.
   Candidates: silent data loss / a turn answered blind = HIGH at least; a known
   gap the PR itself admits but does not test = MEDIUM, never LOW; `--no-verify`
   in the PR's commits = a finding.
3. **Read the PR body against the diff.** Counts and claims in the description
   ("21 ignores") are checkable; a mismatch is a finding.
4. **Cross-PR context (gatherer):** list other open PRs to the same base that
   touch the same files or bump the same version string, and feed them in.
5. **Track the verdict distribution** (the dashboard already exists): a gate whose
   BLOCK/WARN rate sits at 0% for weeks raises its own flag.

Done when a planted-defect PR comes back BLOCK (or WARN at least) and a clean PR
still comes back APPROVE — both, same prompt version.

## Receipts

- **server-bot#102** (2026-09-25): BLOCK/CRITICAL — but **does not count**. The diff
  carries a `# DELIBERATE BUG` comment and the title/body announce the test; BAIR
  cited the label. A labeled defect proves nothing.
- **server-bot#103** (2026-09-25, blind, closed): #92 fix reverted as a plausible
  refactor ("one request builder for submit and resume"), guarding test deleted,
  version bumped, no hint anywhere. Verdict **BLOCK/CRITICAL**, exit 1: named the
  #92 regression, flagged the test deletion as fake-green, asked for the #general
  image probe. Caveat: the removed lines included the comment that explained the
  fix, so this is the easy case.

**Still open for step 1:** (a) a blind defect with no explanatory trace in the diff
(e.g. the #94 double-fold hole); (b) a clean PR on the same prompt version still
comes back APPROVE. Only then close step 1 and move to calibration.

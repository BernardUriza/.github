# 01 — The gate has never said no: prove BAIR can fail, then tune it

Status: **In progress** 2026-09-25 — steps 1 and 2 done (done-criterion met); steps 3–5 open (from a review of server-bot PRs #93/#94/#95).

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

- **server-bot#102, v1** (2026-09-25 22:46): BLOCK/CRITICAL — **does not count**. The
  diff carried a `# DELIBERATE BUG` comment and title/body announced the test; BAIR
  cited the label. A labeled defect proves nothing.
- **server-bot#102, v2** (23:00, rewritten blind by another session, closed): `_payload`
  stops persisting reference attachments (#50 undone). Verdict **WARN/HIGH**, check
  green: mergeable. Diagnosis correct.
- **server-bot#103** (2026-09-25, blind, closed): #92 fix reverted as a plausible
  refactor ("one request builder for submit and resume"), guarding test deleted,
  version bumped, no hint anywhere. Verdict **BLOCK/CRITICAL**, exit 1: named the
  #92 regression, flagged the test deletion as fake-green, asked for the #general
  image probe. Caveat: the removed lines included the comment that explained the
  fix, so this is the easy case.
- **server-bot#104** (2026-09-25, blind, closed): `all(_is_reference)` → `any(...)` in
  `_payload`, sold as "keep references when mixed with inline blocks". Real damage:
  (1) a mixed turn writes MBs of inline base64 into the Postgres row — what the
  row was designed never to hold; (2) `has_attachments` goes False, so the job
  looks resumable, and on resume `_expired` does `block["source"]["url"]` on the
  inline block → `KeyError`, the resume crashes. No test covers the mixed case, so
  CI stays green. Verdict **WARN/HIGH**, check **green → mergeable**. It spotted
  that the semantics changed and that no test covers it, but got the direction
  wrong ("inline blocks may be lost" — they are persisted, not lost), missed the
  base64-in-the-row cost, and missed the resume crash entirely.

## What step 1 proved

- BAIR **can** say BLOCK — when the defect is spelled out in the diff (#103: the
  removed comment explained the fix it reverted).
- On the two subtle blind defects (#102 v2, #104) it lands on **WARN/HIGH, which
  does not gate** — both would have merged. HIGH-severity correctness findings
  that don't block are the calibration hole: step 2 is now evidence-driven, not a
  hunch. Candidate rule: a HIGH on a data-plane path (persistence, resume,
  attachments) with no covering test = BLOCK.
- It reasons from the diff plus rules, not by tracing call sites: the `_expired`
  crash needed reading `_resumer`, which is outside the hunk. Feeding the bodies
  of functions that consume a changed return value (callers of `_payload`'s
  output) is a gatherer candidate alongside cross-PR context.


## Step 2 — severity calibration (2026-09-25, BernardUriza/.github `780a4ac`)

- `gatekeep_system.md`: a **data-plane** section. A change that can drop, duplicate,
  corrupt or mis-persist data, store what must not be stored, or crash the reader
  of a stored record is CRITICAL; so is deleting the test that guards it, and so
  is a data-plane behavior change with no covering test. When the diff cannot show
  the reader, that uncertainty is the finding (name the reader and the test).
- `gatekeep.py::_floor_verdict`: the verdict never sits below its most severe issue
  (CRITICAL → BLOCK, HIGH → at least WARN), whatever the model wrote. Tests in
  `bair/tests/test_gatekeep_verdict_floor.py`.

Same prompt version, all runs 23:09–23:14 UTC:

| PR | Kind | Before | After |
|----|------|--------|-------|
| #104 | planted, blind, data plane | WARN/HIGH, green | **BLOCK/CRITICAL**, red; diagnosis now right (base64 lands in the row, `has_attachments=False` makes it look resumable) |
| #101 | real, clean, not data plane | APPROVE/LOW | **APPROVE/LOW** |
| #105 | real, clean, **data plane** (drops the base64 image path, tests updated) | — | **APPROVE/LOW**, no false positive |

Done-criterion of this item met. Still not seen: the `_expired` `KeyError` on resume,
which lives outside the hunk — that is step 4's job (feed the readers of changed
data), together with cross-PR context. Watch the verdict distribution (step 5) for
false positives on data-plane PRs over the next weeks before trusting the rule.

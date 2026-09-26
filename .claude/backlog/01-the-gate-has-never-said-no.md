# 01 — The gate has never said no: prove BAIR can fail, then tune it

Status: **In progress** 2026-09-26 — eval suite live; first baseline says **recall is the problem** (held-out 0/6 BLOCKed, 3–4/6 flagged), precision is fine (0/12 false BLOCKs) (from a review of server-bot PRs #93/#94/#95).

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

~~Done-criterion of this item met.~~ **Retracted 2026-09-26:** one run per case is not
evidence. 1/1 and 2/2 have 95% Wilson lower bounds of ~21% and ~34% — compatible
with a coin flip, and LLM verdicts flip between runs even at temperature 0. The
done-criterion is replaced by step 6.

## Step 4a — changed-code context (`f0669ce`)

`bair/gatherers/changed_context.py` adds a `<changed_code_context>` block: full
post-change text of every touched file plus `git grep -w` call sites of each changed
function (the one ENCLOSING the changed line — git's hunk header names the def
before the hunk). Reruns at 23:27 UTC: #101 and #105 still APPROVE/LOW, but each
loaded **~80 KB, the full budget**. AACR-Bench (below) shows plain LLM calls get
worse as raw context grows, while targeted slicing helps — so the next iteration is
slicing (changed functions + callers' bodies + readers of the same data), measured
against full-file context in the eval suite, not assumed.

## Research verdict (2026-09-26, /histerical-search, 4 parallel investigators)

- **Planting defects:** replaying real bug-introducing commits is the best-validated
  method (Greptile); trivial synthetic mutants overstate ability up to 12× (F1 0.847
  synthetic vs 0.066 real, arXiv 2606.15689); label leakage ("// Here is the bug") is
  documented (arXiv 2606.29088) — exactly #102 v1. Detection drops ~15× from <10-line
  to >150-line diffs: our planted defects were small and isolated, the easy case.
- **Real accuracy:** independent recall 10–45%, precision single digits to ~40%
  (AACR-Bench arXiv 2601.19494, CR-Bench arXiv 2603.11078); vendor figures do not
  survive re-scoring (Greptile 82% → 45%). Nobody has studied out-of-hunk effects
  specifically.
- **Blocking gates:** no major vendor blocks by default — Anthropic Code Review's
  check is always `neutral`, Bugbot/Copilot advisory; CodeRabbit blocks opt-in with
  a graduated warning→error rollout and an override command. Anthropic's advice:
  gate in your own CI, on top-severity findings only.
- **Eval design:** binary labels, per-class rates (defects caught / clean falsely
  blocked), K runs per case, paired old-vs-new comparison, a held-out set never used
  for tuning, rerun on every prompt change (Anthropic docs + Miller arXiv 2411.00640,
  OpenAI eval guide, Hamel Husain, Eugene Yan).

## Step 2b — shadow mode (`aec1bb3`)

Following the graduated-rollout practice: data-plane findings carry
`"type": "data_plane"` with a file:line citation, and a BLOCK that rests only on
them exits as **WARN with `would_block=true`** (comment banner + `$GITHUB_OUTPUT`).
Any CRITICAL outside the shadow set still blocks. Promotion = emptying
`_SHADOW_TYPES` in `gatekeep.py`, allowed only when step 6 passes.

Live receipt (#104 rerun, 00:27 UTC, context + shadow): **WARN, severity CRITICAL,
`would_block` banner, exit 0**. With the full file in view it now cites the module
docstring's contract ("inline adjuntos no se persisten") and names `_resumer`
skipping the `NotResumableError` guard for inline content — the out-of-hunk reader.
It still does not name the `_expired` `KeyError` itself; one run, so no conclusion
beyond "the plumbing works".

## Step 6 — the eval suite (gates promotion out of shadow)

1. **Cases:** 12 planted defects (distinct classes; prefer replays of real fix
   commits; at least 3 buried in plausible multi-file diffs; at least 3 whose effect
   lands outside the hunk) + 12 clean real PRs, including hard negatives (large
   refactors, legitimate test deletions, dependency bumps, clean data-plane changes
   like #105). Each defect proven by a hidden test or repro script. A leakage lint
   rejects any diff/title/branch/commit containing bug/deliberate/planted/test-gate
   wording. Ground truth lives outside the target repo.
2. **Split:** 6+6 dev (may be read while editing the prompt), 6+6 held-out (only
   aggregates are read; a held-out case used for tuning moves to dev and is replaced).
3. **Runs:** 5 per case per prompt version, production settings; majority verdict.
4. **Metrics:** defect recall (majority BLOCK or would_block), false-block rate,
   flip rate, each with a Wilson 95% interval; paired discordant cases old vs new.
5. **Pass (held-out):** 0 clean cases with majority BLOCK and none BLOCKed in >1/5
   runs; ≥5/6 defects caught; no defect the previous version caught regresses
   (#103-class must stay BLOCK); flip rate ≤2 cases.
6. **Versioning:** results as JSONL keyed by sha256 of `gatekeep_system.md` + model
   id; `@pytest.mark.eval` suite triggered when the prompt or gatekeep code changes;
   a baseline file per prompt version.
7. **Growth:** every real miss or false BLOCK in production becomes a labeled case.
   12+12 is a regression tripwire, not a precision estimate (±10 points needs 50+
   per class).

Also open from the research: a logged override (`/bair override <reason>` or a
maintainer-only label) before anything leaves shadow; consider 2-of-3 agreement
before `exit 1` if the suite shows a nonzero flip rate. Injection note: the PR
body/title are NOT sent to the model today (`_build_user_msg` sends repo + number
only); the remaining vector is comments inside the diff.

## Step 6 — built (2026-09-26, BernardUriza/.github `5e9d5ab`)

- **Cases** (`bair/src/bair/evals/cases_server_bot.json`): 12 defects = real
  bug-introducing commits of server-bot. #92's culprit c74dbf4 comes from its PR
  body; the other 11 come from **SZZ** (Śliwerski/Zimmermann/Zeller 2005: blame, on
  the fix's parent, the lines each `fix` commit changed), filtered from 60
  candidates by hand — SZZ's documented noise (prompt tweaks, lint, deps, features)
  dropped. Ground truth = the fix commit and its message. 12 clean = 10 commits
  sampled with `random.seed(42)` among code commits (40–400 lines, ≥2 .py files)
  never blamed by SZZ, plus merged #101 and #105 (clean data-plane hard negative).
  Clean labels are approximate: a commit nobody fixed later can still hide a bug.
  Split 6+6 dev / 6+6 held-out; the two data-plane defects go one to each side.
- **Runner** `python -m bair.evals`: same `gatekeep.review` as the live gate (now
  extracted from `gatekeep()`), a git worktree per case, JSONL rows keyed by the
  prompt's sha256 + model + bair ref, report with Wilson intervals, flip rate,
  localization (does any issue name a file the real fix touched), and paired
  discordance. Held-out cases are reported only in aggregate.
- **Context modes** behind `BAIR_CONTEXT`: `full` (default: whole files + call
  sites, median ~70 KB on these cases) vs `slice` (function bodies along the data
  flow: changed functions, callers, same-module readers of the hunk's data keys,
  one hop into helpers; median ~24 KB). On #104 the slice is 4 KB and holds exactly
  `_payload → resume → _expired`.
- **Where it runs**: `server-bot` branch `bair-eval`, workflow
  `.github/workflows/bair-eval.yml`, triggered by push to that branch only (nothing
  on main, no CI/CD). Secrets stay in the consumer repo. Rows ship as the
  `bair-eval-jsonl` artifact.

## Step 6 — first baseline (2026-09-26, server-bot run 36205574236)

prompt `4a487c5ea97e` · model `claude-opus-4-7` · bair `5e9d5abe88e8` · 24 cases × 2
contexts × 3 runs = 144 rows, 0 unavailable, median 6 s per review. Rows:
artifact `bair-eval-jsonl` of that run.

| split | context | defects BLOCKed | defects ≥WARN | clean falsely BLOCKed | clean ≥WARN | flipped |
|---|---|---|---|---|---|---|
| dev | full | 2/6 (10–70%) | 2/6 | 0/6 (0–39%) | 0/6 | 0 |
| dev | slice | 2/6 (10–70%) | 2/6 | 0/6 (0–39%) | 0/6 | 1 |
| **held-out** | full | **0/6** (0–39%) | 3/6 (19–81%) | 0/6 (0–39%) | 0/6 | 0 |
| **held-out** | slice | **0/6** (0–39%) | 4/6 (30–90%) | 0/6 (0–39%) | 1/6 | 1 |

**Decision (rule fixed before the run):** `slice` becomes the default context
(`bair` commit after `5e9d5ab`): on held-out it is non-inferior to `full` on BLOCKs
and false BLOCKs, and ~3× smaller. The 4/6 vs 3/6 at ≥WARN is one case — noise, not
a win; it also added one clean WARN and one flip. `full` stays available via
`BAIR_CONTEXT=full`.

What the baseline says (dev read case by case, held-out only in aggregate):

- **Precision holds, recall does not.** 0 false BLOCKs in 12 clean cases (upper
  bound ~39% at n=6 — a tripwire, not a precision estimate). But the gate BLOCKs 2
  of 12 real regressions, both in dev: #92's culprit (c74dbf4) and the 8000-char
  cap that 422'd attachments (f1208d0). In held-out it BLOCKs none.
- **What it waves through (dev):** a retry catching an exception class the SDK
  never raises for 529, httpx not following Azure's 301, a server tool sent with an
  invalid `description`, a fallback that fires on tool-only turns — APPROVE/LOW,
  unanimous across runs. These need library/API knowledge the diff does not carry;
  more repo context does not supply it.
- **Shadow mode is leaky.** Both dev BLOCKs were real (`would_block=false`), typed
  outside `data_plane` — the model does not reliably use the type the prompt asks
  for, so `_SHADOW_TYPES` does not contain the new rules as designed. Here the
  blocks were correct; the containment claim in step 2b is still false.
- **Promotion out of shadow: not met** (needs ≥5/6 held-out defects caught).

Next, evidence-driven: (1) make shadow attribution not depend on the model's free
`type` (e.g. a required `rule` field enumerated in the schema, validated in code);
(2) attack recall on the API/library class — a verification/second-pass that asks
per changed call "what does this library actually do here?" is the candidate the
research points to (Anthropic's verification step, BitsAI-CR filter); measure it
with this suite before shipping; (3) grow n toward 50 per class for real intervals.

## Step 7 — model A/B, claude-opus-4-7 vs claude-opus-5-5 (2026-09-26)

**First attempt void** (run 36209279458): 72/72 opus-5-5 reviews UNAVAILABLE — the
gate read `content[0]["text"]` and Opus 5.5 always thinks, so a `thinking` block
comes first. A production bug waiting for the first model bump; fixed in `9ea197b`
(join `text` blocks; `raw_decode` the first JSON object — 1/72 opus-4-7 reviews had
also failed on trailing prose). Same run showed the enumerated `rule` works: 94/94
issues carried a valid value.

**Valid run** 36209965979 · prompt `5d1c3545aae6` · bair `9ea197b` · slice context ·
3 runs · 144 rows · 0 unavailable · rule compliance 341/341 · median 6 s (4-7) vs
17 s (5-5). Both at production defaults: 4-7 runs without thinking, 5-5 with
adaptive thinking at its default effort (`medium`) — the comparison is
"model + its default thinking", not the model alone.

| split | config | defects BLOCKed | defects ≥WARN | clean falsely BLOCKed | clean ≥WARN |
|---|---|---|---|---|---|
| dev (as labelled) | 4-7 | 2/6 | 2/6 | 0/6 | 0/6 |
| dev (as labelled) | 5-5 | 2/6 | 4/6 | 1/6 | 2/6 |
| **held-out** | 4-7 | **0/6** | 2/6 | **0/6** | 2/6 |
| **held-out** | 5-5 | **4/6** (30–90%) | 4/6 | **3/6** (19–81%) | 4/6 |

**Pre-registered rule: switch only if held-out BLOCKs ≥, ≥WARN ≥, and no added false
BLOCKs → FAILS on false BLOCKs (3/6 vs 0/6). The default stays `claude-opus-4-7`.**

**But the one dev "false BLOCK" was a mislabel.** `clean-13884b5` (agent_facts MCP
tools): opus-5-5 said 3/3 runs that `update_agent_fact` scopes its UPDATE/soft-delete
by fact id only, never by `agent_id`, so Insult can rewrite ALICE's self-facts — which
the PR itself forbids, guarded only by a persona instruction. Verified by hand, and
**still live on server-bot main** (`persona_runner/mcp_tools/agent_facts.py:111`).
Relabelled as a latent defect (`defect-13884b5`, split dev), per the documented
practice of adding latent defects to the ground truth rather than scoring them as
false positives. Dev rescored with the correct label: 4-7 BLOCKs 2/7, flags 2/7,
0/5 false BLOCKs; 5-5 BLOCKs 3/7, flags 5/7, **0/5 false BLOCKs**.

That makes the held-out 3/6 suspect in the same way, and the decision hinges on it.
Next: a **label audit of the three held-out clean cases opus-5-5 BLOCKed** — reading
them spends them as held-out, so each audited case moves to dev and a fresh clean
case replaces it in held-out; the A/B then reruns on the rebuilt held-out.

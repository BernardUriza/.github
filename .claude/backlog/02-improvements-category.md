# 02 — An `improvements` category: say what would make the PR better, not only what breaks it

Status: **Proposed** 2026-09-25 (Bernard: "agregar la categoría de mejoras, tipo lo que hace /insult en su prompt").

## Why

`/insult` (`~/.claude/commands/insult.md`) works in two phases: **attack** (what is
wrong) and then **improvement** (the concrete refactor, the missing test, the
simplification), and it always moves from the first to the second. BAIR only does
the first. Its schema has `issues` (defects) plus one free-text `recommendation`, so
anything that is not a defect has only two places to go:

- a LOW `issue`, which dilutes severity. It is part of why 35 of 40 server-bot PRs
  came back APPROVED/LOW (see [01](01-the-gate-has-never-said-no.md)): a real defect
  and a style nit look identical;
- or nowhere, and the review ends with nothing a developer can act on.

## The change

1. **Schema** (`bair/src/bair/prompts/gatekeep_system.md`): add a separate array
   `"improvements": [{"kind": "refactor|test|simplify|performance|observability|docs", "where": "file:line", "change": "the concrete edit, not advice", "why": "one line"}]`.
2. **The contract, written into the prompt:**
   - improvements **never** move the verdict or the severity;
   - `issues` is only for defects, and a style nit is an improvement, not a LOW issue;
   - each one names the exact edit the way `/insult` phase 2 does ("replace X with Y at
     file:line"), never "consider improving readability";
   - at most ~3 improvements, highest value first. Few and concrete beats a long list.
3. **Pipeline** (`bair/src/bair/pipelines/gatekeep.py`):
   - `GatekeepDecision` gets `improvements: list[dict]` (line 63);
   - all three providers read it with `parsed.get("improvements", [])` (lines 240, 273, 307);
   - `_render_comment` (line 337) adds a `### Improvements` section after the issues;
   - `_abstain` returns `improvements=[]`.
4. **Tests:** one that parses a response with improvements and renders the section;
   one resistance test showing a response with improvements and no issues still
   gets APPROVE (improvements must not raise the verdict).

## What NOT to copy from /insult

The tone (insults, profanity, character) and "fix it yourself". BAIR comments on
PRs other people open, and the gate is read-only: it proposes the edit and does not
push it.

## Relation to 01

Do it together with the severity calibration in 01. Once style has somewhere to go
(`improvements`), `issues` stays clean, and 01's red test measures exactly that: a
planted defect has to come back as an issue with real severity, not buried among the
nits.

Done when a real server-bot PR shows an `### Improvements` section with concrete
edits, and a PR with improvements and no defects is still APPROVE.

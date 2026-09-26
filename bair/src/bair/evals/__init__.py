"""BAIR eval suite — measure the gatekeeper on labeled cases before trusting a change.

Cases are (base, head) pairs in a target repo's history: real bug-introducing
commits found by SZZ (blame the lines each fix changed) and verified by hand, plus
clean commits never blamed by a fix. Each case is reviewed through the same
``gatekeep.review`` the live gate uses, N times per context mode, and every run is
one JSONL row keyed by the sha256 of the system prompt. See BernardUriza/.github
``.claude/backlog/01-the-gate-has-never-said-no.md`` (step 6) for the protocol.
"""

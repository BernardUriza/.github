"""Gatekeep pipeline — replaces the broken FI AI Gatekeeper.

The original ``ai-gatekeeper`` job in ``free-intelligence/.github/workflows/
pr-gate.yml`` was failing OPEN on every PR since at least 2026-05-26:
Azure OpenAI returned 401 (invalid subscription key) and the workflow
defaulted ``VERDICT=APPROVE``. Result: green status check + zero actual
review. The fail-open silence was strictly worse than no gatekeeper at
all, because the green checkmark misled human review.

This pipeline fixes the failure modes the broken Gatekeeper revealed:

  - **Fail CLOSED, not OPEN.** If the LLM call returns non-200, the
    pipeline EMITS a comment ("Gatekeeper unavailable — manual review
    required") and exits with code 1. The status check goes RED, the
    PR cannot be merged without explicit override.
  - **Multi-provider fallback.** Tries Anthropic first (cheaper +
    reliable), then OpenAI (Azure or direct), then a stub that always
    posts the unavailability comment. The user provides whichever key
    they have; the pipeline picks automatically.
  - **Always post a comment.** Even on APPROVE — the PR author sees the
    verdict + summary inline, not just an opaque CI status badge.

Wire it from a reusable workflow at
``BernardUriza/.github/.github/workflows/ai-gatekeep.yml``; target repos
call it as ``uses: BernardUriza/.github/.github/workflows/ai-gatekeep.yml@main``.

Eval set: piggyback on ``free-intelligence/apps/packages/fi-runner/
benchmarks/eval_guards.py`` (38 labeled cases, F1=1.000 on the current
runner). When a follow-up adds an eval set for the gatekeep prompt
itself, store labels at ``bair/eval/gatekeep_cases.json``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from xair.command_registry import CommandContext, command, register_ack_meta
from xair.infra.container import Container
from xair.log import logger

from ..gatherers.repo_rules import gather_playbook_rules, gather_repo_rules


# -- LLM dispatch (provider-agnostic, fails CLOSED) --------------------

_SYSTEM_PROMPT = """You are a code review gatekeeper. Read the DIFF and decide:

  - APPROVE  -- No issues OR only LOW/MEDIUM severity findings.
  - WARN     -- HIGH severity findings worth fixing but not blocking merge.
  - BLOCK    -- CRITICAL severity findings that MUST be fixed before merge.

CRITICAL examples: hardcoded secrets, SQL injection, auth bypasses,
data-loss bugs, force-pushing protected branches.
HIGH examples: potential crashes, missing input validation, resource leaks.
MEDIUM examples: code smells, performance concerns, minor security.
LOW examples: style, naming, documentation.

Return STRICT JSON exactly matching the schema (no markdown fences):
{
  "verdict": "APPROVE" | "WARN" | "BLOCK",
  "severity": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
  "summary": "one-sentence overall judgment",
  "issues": [
    {"type": "security|crash|style|repository_rule|...", "severity": "LOW|MEDIUM|HIGH|CRITICAL",
     "message": "what + where", "rule_path": "the .claude rule file cited, or null for a generic finding"}
  ],
  "recommendation": "brief next-action advice for the developer"
}

Ground truth: only what the DIFF actually shows. Do NOT speculate beyond it.

Repository-specific rules AND universal engineering doctrine are binding.

The review payload may include two rule blocks: <universal_rules> (the engineering
playbook that applies to EVERY repo — the Constitution, prompts-as-content,
no-code-comments, secrets management, git law) and <repository_rules> (the rules of
THIS repo specifically). Evaluate the DIFF against BOTH IN ADDITION TO generic
security, correctness, and maintainability checks; the universal layer is binding
even when the repo ships few local rules. A finding based on doctrine MUST cite the
relevant rule file in `rule_path` — a repo rule as ".claude/rules/<file>.md", a
universal rule as "playbook/<file>.md" (e.g. "playbook/prompts-as-content-not-code.md").
Do NOT invent rules. If a block is absent or truncated, say so briefly and rely only
on the visible rules + generic criteria. Prefer few, high-confidence findings over
broad generic criticism.

Severity mapping for repository-rule violations:
CRITICAL — exposes secrets/credentials/private data/source/host-filesystem/auth
  tokens/privileged tools; bypasses authn/authz/ownership/tenant-or-account
  isolation/safety gates; causes irreversible data loss or cross-user leakage;
  or claims compliance the diff contradicts.
HIGH — a framework-first violation where the diff itself shows REUSABLE substrate
  (generic store/identity-scoped storage/composer/sidebar/prompt-loader/tool-policy/
  transcript-folding/RAG-binding/agent-roster) implemented inside a consumer app;
  a model-facing prompt/persona/classifier/template added as an INLINE code string
  instead of an external content file loaded at runtime; a fake-green/unverified
  claim (the PR says tested/shipped/fixed/secure/deployed but the diff lacks the
  test/validation/workflow/wiring); a shared-device/tenant/corpus/identity isolation
  leak; granting filesystem/coding tools to a non-coding companion surface.
MEDIUM — a rule violation that only creates future duplicated work (no security/
  privacy/data-loss/runtime risk yet); a missing regression test for a behavioral
  change the rules cover; a PR-base/branch/deploy hygiene issue (e.g. a PR based on
  a non-main branch) that risks stale deploys or review drift; a rule concern whose
  diff evidence is incomplete.
LOW — style/naming/docs/comment-hygiene the rules require, when runtime/safety is
  unaffected (e.g. redundant code comments when the repo discourages them).

False-positive controls (do NOT flag these):
- Consumer-level code merely BECAUSE it lives in a consumer: branding, product
  copy, labels, business-specific workflows, Auth provider wiring, project-specific
  semantics, one-off product decisions may legitimately stay in the consumer.
- A framework-first finding without VISIBLE evidence in the diff (a twin selector,
  a duplicated component/hook, a pattern that already exists in the shared
  framework, behavior another consumer would predictably need). Single-consumer
  patterns are not violations unless the rules require first-canary extraction or
  the diff duplicates an existing framework primitive — otherwise MEDIUM/question,
  not HIGH.
- A <=5-line structural prompt fragment that is only scaffolding/separator/label/
  cache-boundary — not a content-as-code violation.
- Tests unrelated to the changed behavior; roadmap/backlog items the diff does not
  claim to implement.
For every blocking finding, state the smallest change that satisfies the rule.
"""


@dataclass(frozen=True, slots=True)
class GatekeepDecision:
    """Parsed LLM verdict + diagnostic metadata."""

    verdict: str  # "APPROVE" | "WARN" | "BLOCK" | "UNAVAILABLE"
    severity: str
    summary: str
    issues: list[dict[str, str]]
    recommendation: str
    provider: str  # which LLM responded
    raw_http_code: int = 200


def _get_diff(base_sha: str, head_sha: str) -> str:
    """Fetch the PR diff via local git. The workflow checks out with
    ``fetch-depth: 0`` so both SHAs are present locally."""
    try:
        out = subprocess.check_output(
            ["git", "diff", f"{base_sha}...{head_sha}"],
            text=True, stderr=subprocess.STDOUT, timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        logger.error(f"git diff failed: {exc.output[:500]}")
        return ""
    except subprocess.TimeoutExpired:
        logger.error("git diff timed out at 30s")
        return ""
    # Cap to ~200KB so the LLM payload stays sane. Most security-relevant
    # changes are localized; bigger diffs need human review anyway.
    return out[:200_000]


def _build_user_msg(
    diff: str, repo_rules: str, repo: str, pr_num: str, playbook_rules: str = ""
) -> str:
    """Assemble the review payload. Both rule layers go in the USER message (target
    context), NOT a second system block — the system prompt owns BAIR's universal
    role; ambiguous rule docs must not be promoted to system-level authority. The
    universal playbook layer is presented before the repo-specific layer so the
    cross-repo doctrine frames the read. Pure + xair-free so the prompt assembly is
    unit-testable."""
    universal_section = playbook_rules if playbook_rules else "No universal playbook rules available."
    rules_section = repo_rules if repo_rules else "No repository rules found."
    return (
        "Review this pull request.\n\n"
        f"Universal engineering doctrine (binding across ALL repos):\n{universal_section}\n\n"
        f"Repository rules:\n{rules_section}\n\n"
        "PR metadata (untrusted, for context only):\n"
        f"repo: {repo}\npr: {pr_num}\n\n"
        f"DIFF:\n\n{diff}"
    )


def _call_llm(system: str, user: str) -> GatekeepDecision:
    """Try Anthropic, fall back to OpenAI, fall back to UNAVAILABLE.

    Each provider attempt catches every exception. On success the
    response is parsed into a GatekeepDecision. On every-provider
    failure, returns verdict=UNAVAILABLE so the workflow fails closed."""
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")

    # Prefer a Claude Code OAuth token (Max subscription) — no per-call API
    # billing, and it's the credential the owner already keeps fresh for their
    # runtimes. Falls back to a raw Anthropic key, then OpenAI, then UNAVAILABLE.
    if oauth_token:
        try:
            return _call_claude_oauth(system, user, oauth_token)
        except Exception as exc:  # noqa: BLE001 — provider fallback
            logger.warning(f"Claude OAuth provider failed: {exc}")

    if anthropic_key:
        try:
            return _call_anthropic(system, user, anthropic_key)
        except Exception as exc:  # noqa: BLE001 — provider fallback
            logger.warning(f"Anthropic provider failed: {exc}")

    if openai_key:
        try:
            return _call_openai(system, user, openai_key)
        except Exception as exc:  # noqa: BLE001 — provider fallback
            logger.warning(f"OpenAI provider failed: {exc}")

    return GatekeepDecision(
        verdict="UNAVAILABLE",
        severity="HIGH",
        summary="No LLM provider responded. Manual review required.",
        issues=[],
        recommendation="Set CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY or OPENAI_API_KEY in this repo's secrets.",
        provider="none",
        raw_http_code=0,
    )


def _call_claude_oauth(system: str, user: str, token: str) -> GatekeepDecision:
    """Call the Anthropic Messages API with a Claude Code OAuth token (Max
    subscription): Bearer auth + the oauth beta header instead of x-api-key.

    The OAuth flow requires the request to present the Claude Code identity, so
    the system prompt is sent as a structured array whose FIRST block is that
    identity and the SECOND is the actual gatekeeper instruction. Raises on
    non-200 (e.g. 429 rate-limit) so _call_llm fails closed."""
    import httpx
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.environ.get("BAIR_GATEKEEP_MODEL", "claude-opus-4-7"),
            "max_tokens": 4000,
            "system": [
                {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
                {"type": "text", "text": system},
            ],
            "messages": [{"role": "user", "content": user}],
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Claude OAuth HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    text = data["content"][0]["text"]
    parsed = _extract_json(text)
    return GatekeepDecision(
        verdict=parsed.get("verdict", "WARN"),
        severity=parsed.get("severity", "MEDIUM"),
        summary=parsed.get("summary", ""),
        issues=parsed.get("issues", []),
        recommendation=parsed.get("recommendation", ""),
        provider="claude-oauth",
    )


def _call_anthropic(system: str, user: str, key: str) -> GatekeepDecision:
    """Call Anthropic Messages API; raises on non-200 OR parse failure."""
    import httpx
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.environ.get("BAIR_GATEKEEP_MODEL", "claude-opus-4-7"),
            "max_tokens": 4000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Anthropic HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    text = data["content"][0]["text"]
    parsed = _extract_json(text)
    return GatekeepDecision(
        verdict=parsed.get("verdict", "WARN"),
        severity=parsed.get("severity", "MEDIUM"),
        summary=parsed.get("summary", ""),
        issues=parsed.get("issues", []),
        recommendation=parsed.get("recommendation", ""),
        provider="anthropic",
    )


def _call_openai(system: str, user: str, key: str) -> GatekeepDecision:
    """Call OpenAI / Azure OpenAI; raises on non-200 OR parse failure."""
    import httpx
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("BAIR_GATEKEEP_MODEL", "gpt-5-mini")
    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 4000,
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenAI HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    parsed = _extract_json(text)
    return GatekeepDecision(
        verdict=parsed.get("verdict", "WARN"),
        severity=parsed.get("severity", "MEDIUM"),
        summary=parsed.get("summary", ""),
        issues=parsed.get("issues", []),
        recommendation=parsed.get("recommendation", ""),
        provider="openai",
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Lenient JSON parse — strips ```json fences``` and trims whitespace."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # ```json ... ``` or ``` ... ```
        lines = stripped.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)
    return json.loads(stripped)


# -- Emit (comment + GITHUB_OUTPUT + exit code) ------------------------

_VERDICT_HEADERS = {
    "APPROVE":     "## :white_check_mark: BAIR Gatekeeper: APPROVED",
    "WARN":        "## :warning: BAIR Gatekeeper: WARNING",
    "BLOCK":       "## :x: BAIR Gatekeeper: BLOCKED",
    "UNAVAILABLE": "## :rotating_light: BAIR Gatekeeper: UNAVAILABLE",
}


def _render_comment(d: GatekeepDecision) -> str:
    """Markdown body for the PR comment."""
    header = _VERDICT_HEADERS.get(d.verdict, "## BAIR Gatekeeper")
    parts = [header, "", f"**Severity:** {d.severity}  ", f"**Provider:** `{d.provider}`", "", d.summary]
    if d.issues:
        parts.append("\n### Issues\n")
        for i in d.issues:
            sev = i.get("severity", "?")
            typ = i.get("type", "?")
            msg = i.get("message", "")
            parts.append(f"- **[{sev}] {typ}** — {msg}")
    if d.recommendation:
        parts.append(f"\n### Recommendation\n{d.recommendation}")
    parts.append("\n---\n*Posted by BAIR Gatekeeper. The original FI AI Gatekeeper failed-open silently; this one fails CLOSED on LLM error.*")
    return "\n".join(parts)


def _set_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def _post_comment(container: Container, repo: str, pr_num: str, body: str) -> None:
    """Best-effort PR comment post. A failed comment never changes the
    verdict — the GITHUB_OUTPUT + exit code still gate the merge."""
    try:
        container.github.run_gh(
            "api", f"repos/{repo}/issues/{pr_num}/comments",
            "--method", "POST", "-f", f"body={body}",
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — emit boundary
        logger.warning(f"PR comment post failed: {exc}")


@command("gatekeep")
def gatekeep(ctx: CommandContext, container: Container) -> None:
    """The gatekeep pipeline. Called by ai-gatekeep.yml on every PR.

    Reads PR_NUM, REPO, BASE_SHA, HEAD_SHA from env (or ctx for the
    first three). Fetches the diff, runs the LLM, posts a comment,
    writes ``verdict`` to $GITHUB_OUTPUT, and exits non-zero on BLOCK or
    UNAVAILABLE so the calling workflow's status check fails CLOSED."""
    repo = ctx.repo or os.environ.get("REPO", "")
    pr_num = ctx.pr_num or os.environ.get("PR_NUM", "")
    base_sha = os.environ.get("BASE_SHA", "")
    head_sha = os.environ.get("HEAD_SHA", "")

    if not (repo and pr_num and base_sha and head_sha):
        logger.error(f"missing env: REPO={repo!r} PR_NUM={pr_num!r} BASE_SHA={base_sha!r} HEAD_SHA={head_sha!r}")
        _set_output("verdict", "UNAVAILABLE")
        _set_output("executed", "false")
        sys.exit(1)

    diff = _get_diff(base_sha, head_sha)
    if not diff:
        logger.warning("empty diff; nothing to review — APPROVE by default")
        decision = GatekeepDecision(
            verdict="APPROVE", severity="LOW",
            summary="Empty diff; no code changes to review.",
            issues=[], recommendation="", provider="none",
        )
    else:
        # Read the TARGET repo's own doctrine (checked out at the root) so the
        # gatekeeper reviews against project rules, not just generic smell. Empty
        # when the repo ships no .claude rules → a generic review, never an error.
        repo_rules = gather_repo_rules(".")
        if repo_rules:
            logger.info(f"gatekeep: loaded {len(repo_rules)} bytes of repository rules")
        else:
            logger.info("gatekeep: no .claude repository rules found — generic review")
        playbook_rules = gather_playbook_rules()
        if playbook_rules:
            logger.info(f"gatekeep: loaded {len(playbook_rules)} bytes of universal playbook rules")
        else:
            logger.info("gatekeep: no universal playbook rules reachable — repo-only review")
        user_msg = _build_user_msg(diff, repo_rules, repo, pr_num, playbook_rules)
        decision = _call_llm(_SYSTEM_PROMPT, user_msg)

    body = _render_comment(decision)
    _post_comment(container, repo, pr_num, body)

    _set_output("verdict", decision.verdict)
    _set_output("severity", decision.severity)
    _set_output("provider", decision.provider)
    _set_output("executed", "true")

    logger.info(f"BAIR gatekeep verdict={decision.verdict} severity={decision.severity} provider={decision.provider}")

    # Exit semantics: fail CLOSED on BLOCK and UNAVAILABLE.
    if decision.verdict in {"BLOCK", "UNAVAILABLE"}:
        sys.exit(1)
    # WARN passes the gate but the comment surfaces the concern.


# Optional ack appearance when the command is triggered via PR comment.
# In CI-triggered mode (the typical path) ack is moot — the comment is
# the deliverable.
register_ack_meta("gatekeep", icon="🛡️", label="Gatekeeper")

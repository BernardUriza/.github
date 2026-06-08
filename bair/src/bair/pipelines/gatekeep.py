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
from xair.contracts import GitHubClient
from xair.infra.container import Container
from xair.log import logger


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
    {"type": "security|crash|style|...", "severity": "LOW|MEDIUM|HIGH|CRITICAL", "message": "what + where"}
  ],
  "recommendation": "brief next-action advice for the developer"
}

Ground truth: only what the DIFF actually shows. Do NOT speculate beyond it.
"""


# -- Repo-specific rule ingestion --------------------------------------
#
# bair's central prompt above is intentionally generic. Each target repo
# can EXTEND the gatekeeper with its own rules by committing markdown
# under ``.claude/rules/*.md`` (and optionally ``.github/instructions/
# *.md``). During gatekeep we read those files AT THE PR HEAD SHA via the
# GitHub contents API and fold them into the system prompt as
# "repository-specific gatekeeping rules". If the paths don't exist we
# behave exactly as before — no regression, no central rule is hardcoded.

# Directories scanned for repo-specific rules, in priority order. Only
# top-level ``*.md`` files in each directory are read (no recursion) to
# keep the GitHub API cost bounded and predictable.
_RULE_DIRS = (".claude/rules", ".github/instructions")

# Hard cap on the total injected rule text. A repo with a huge rules dir
# must not be able to blow the LLM context window. ~32k chars ≈ 8k tokens
# of headroom for the diff + system prompt.
_MAX_RULE_CHARS = 32_000


def _list_rule_files(github: GitHubClient, repo: str, directory: str, ref: str) -> list[str]:
    """List top-level ``*.md`` paths in ``directory`` at ``ref``.

    Returns [] if the directory is absent or the API call fails — a
    missing rules dir is the common case and must be silent."""
    try:
        raw = github.run_gh(
            "api", f"repos/{repo}/contents/{directory}?ref={ref}",
            "--jq", '.[] | select(.type=="file") | .path',
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort gather
        logger.debug(f"[gatekeep] listing {directory} failed: {exc}")
        return []
    if not raw.strip() or "Not Found" in raw or "API rate limit" in raw:
        return []
    return [p for p in raw.strip().splitlines() if p.endswith(".md")]


def _fetch_rule_file(github: GitHubClient, repo: str, path: str, ref: str) -> str:
    """Fetch one file's raw content at ``ref``. Returns "" on failure."""
    try:
        content = github.run_gh(
            "api", f"repos/{repo}/contents/{path}?ref={ref}",
            "-H", "Accept: application/vnd.github.raw",
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort gather
        logger.debug(f"[gatekeep] fetching {path} failed: {exc}")
        return ""
    if not content.strip() or content.lstrip().startswith('{"message":"Not Found"'):
        return ""
    return content


def gather_repo_rules(github: GitHubClient, repo: str, ref: str) -> str:
    """Read the target repo's own gatekeeping rules at ``ref`` and render
    them as a prompt fragment. Returns "" when no rules are present, so
    the caller can leave the system prompt untouched.

    Robustness contract:
      - Missing dirs / failed API calls → "" (no regression).
      - Total injected text capped at ``_MAX_RULE_CHARS``; once the budget
        is exhausted, remaining files are skipped and a truncation note is
        logged + appended so the model knows rules were dropped."""
    sections: list[str] = []
    total = 0
    truncated = False

    for directory in _RULE_DIRS:
        for path in _list_rule_files(github, repo, directory, ref):
            content = _fetch_rule_file(github, repo, path, ref)
            if not content.strip():
                continue
            header = f"### {path}\n"
            chunk = header + content.strip() + "\n"
            if total + len(chunk) > _MAX_RULE_CHARS:
                truncated = True
                logger.warning(
                    f"[gatekeep] repo rules exceed {_MAX_RULE_CHARS} char cap; "
                    f"skipping {path} and any further rule files"
                )
                break
            sections.append(chunk)
            total += len(chunk)
        if truncated:
            break

    if not sections:
        return ""

    logger.info(
        f"[gatekeep] ingested {len(sections)} repo rule file(s) "
        f"({total} chars{', TRUNCATED' if truncated else ''}) from {repo}@{ref[:8]}"
    )

    body = "\n".join(sections)
    note = (
        "\n[Note: repository rule text was truncated to fit the context "
        "budget; some rules may be incomplete.]\n"
        if truncated else ""
    )
    return (
        "\n\n--- REPOSITORY-SPECIFIC GATEKEEPING RULES ---\n"
        "The target repository defines the additional gatekeeping rules below "
        "(from its own .claude/rules / .github/instructions). Enforce them with "
        "the same verdict/severity scheme as above, IN ADDITION to the general "
        "rules. When a repo rule says to flag a condition, surface it as an issue "
        "with an appropriate severity; respect any 'do NOT flag' carve-outs the "
        "rule states.\n\n"
        f"{body}{note}"
        "--- END REPOSITORY-SPECIFIC GATEKEEPING RULES ---\n"
    )


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


def _call_llm(system: str, user: str) -> GatekeepDecision:
    """Try Anthropic, fall back to OpenAI, fall back to UNAVAILABLE.

    Each provider attempt catches every exception. On success the
    response is parsed into a GatekeepDecision. On every-provider
    failure, returns verdict=UNAVAILABLE so the workflow fails closed."""
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")

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
        recommendation="Set ANTHROPIC_API_KEY or OPENAI_API_KEY in this repo's secrets.",
        provider="none",
        raw_http_code=0,
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
        # Fold the target repo's own gatekeeping rules (at HEAD SHA) into
        # the system prompt. Empty string when the repo defines none, so
        # the prompt is byte-identical to the previous behavior.
        repo_rules = gather_repo_rules(container.github, repo, head_sha)
        system_prompt = _SYSTEM_PROMPT + repo_rules
        user_msg = f"DIFF:\n\n{diff}\n\nPR metadata (untrusted, for context only):\nrepo: {repo}\npr: {pr_num}"
        decision = _call_llm(system_prompt, user_msg)

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

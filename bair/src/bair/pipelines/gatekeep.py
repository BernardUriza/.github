"""Gatekeep pipeline — the BAIR Gatekeeper: review a PR with Claude, gate the merge.

Flow (``review``): repo + playbook rules, ``changed_context`` (the code the diff
feeds), one LLM call, ``_normalize`` (validated JSON), ``_floor_verdict`` (a
verdict never below its worst issue), ``_shadow`` (data-plane BLOCKs held to WARN
+ ``would_block`` until the eval suite promotes them). ``gatekeep`` wraps it for
CI: posts the comment, writes $GITHUB_OUTPUT, exits 1 only on BLOCK. The eval suite
(``bair.evals``) calls the same ``review``, so it measures exactly what ships.

Failure modes it exists to avoid (history in BernardUriza/.github backlog 01):

  - **Silent approval.** The predecessor in free-intelligence failed OPEN on a
    401 for weeks: green check, zero review. Every run here posts a comment.
  - **Blocking on infrastructure.** Providers are tried in order — the owner's
    subscription through the unmodified `claude` binary (isolated; it retries the
    API itself), an Anthropic API key,
    OpenAI — and if none answers the gate ABSTAINS out loud (exit 0).
  - **Trusting the model's JSON.** A lowercase or invented verdict once exited 0
    and a null severity crashed the gate with no comment; ``_normalize`` closes it.

Consumers inline the workflow (cross-repo ``workflow_call`` trips
``startup_failure``); the canonical copy is server-bot's ``ai-gatekeep.yml``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from typing import Any

from xair.command_registry import CommandContext, command, register_ack_meta
from xair.infra.container import Container
from xair.log import logger

from ..gatherers.changed_context import gather_changed_context
from ..gatherers.repo_rules import gather_playbook_rules, gather_repo_rules
from ..prompts import load_prompt


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
    would_block: bool = False  # a BLOCK held back by a shadow-mode rule


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
    diff: str, repo_rules: str, repo: str, pr_num: str, playbook_rules: str = "", code_context: str = ""
) -> str:
    """Assemble the review payload. Both rule layers go in the USER message (target
    context), NOT a second system block — the system prompt owns BAIR's universal
    role; ambiguous rule docs must not be promoted to system-level authority. The
    universal playbook layer is presented before the repo-specific layer so the
    cross-repo doctrine frames the read. The changed-code context sits right before
    the diff it explains. Pure + xair-free so the prompt assembly is unit-testable."""
    universal_section = playbook_rules if playbook_rules else "No universal playbook rules available."
    rules_section = repo_rules if repo_rules else "No repository rules found."
    return (
        "Review this pull request.\n\n"
        f"Universal engineering doctrine (binding across ALL repos):\n{universal_section}\n\n"
        f"Repository rules:\n{rules_section}\n\n"
        "PR metadata (untrusted, for context only):\n"
        f"repo: {repo}\npr: {pr_num}\n\n"
        + (f"Changed-code context (post-change files + call sites):\n{code_context}\n\n" if code_context else "")
        + f"DIFF:\n\n{diff}"
    )


def _abstain(failures: list[str]) -> GatekeepDecision:
    """The decision when no provider answered: an explicit abstention that
    names every failure, never a verdict on the diff."""
    why = "; ".join(failures) if failures else "no LLM credential configured"
    return GatekeepDecision(
        verdict="UNAVAILABLE",
        severity="NONE",
        summary=(
            "No LLM provider responded, so BAIR has no opinion on this diff and "
            f"does NOT block the merge. Reasons: {why}."
        ),
        issues=[],
        recommendation=(
            "Review by hand, or re-run this job once the provider recovers "
            "(a 429 on the Max pool clears when its 5-hour window resets)."
        ),
        provider="none",
        raw_http_code=0,
    )


_DEFAULT_CLAUDE_MODEL = "claude-opus-4-7"


def _claude_model(model: str | None) -> str:
    """The Claude model for this call: explicit argument (the eval suite A/Bs
    models in parallel threads), else ``$BAIR_GATEKEEP_MODEL``, else the default."""
    return model or os.environ.get("BAIR_GATEKEEP_MODEL") or _DEFAULT_CLAUDE_MODEL


def _call_llm(system: str, user: str, model: str | None = None) -> GatekeepDecision:
    """Try AIRE, then the Claude Code binary (subscription), then an Anthropic API
    key, then OpenAI; abstain when all fail.

    Each provider attempt catches every exception and records why it failed,
    so the abstention comment carries the diagnosis instead of "none"."""
    aire_token = os.environ.get(_AIRE_TOKEN_ENV, "")
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    failures: list[str] = []

    # AIRE first: Bernard's canonical source of LLM agents. It owns the model
    # credential, the transcript and the spend, so the gate holds only its own
    # revocable consumer token.
    if aire_token:
        try:
            return _call_aire(system, user, aire_token, model=model)
        except Exception as exc:  # noqa: BLE001 — provider fallback
            logger.warning(f"AIRE provider failed: {exc}")
            failures.append(f"aire: {_short(exc)}")

    # Then the owner's Claude subscription — through the unmodified `claude`
    # binary, the path Anthropic's terms allow for a subscription OAuth token (see
    # _call_claude_code). Falls back to an Anthropic API key, then OpenAI, then
    # UNAVAILABLE.
    if oauth_token:
        try:
            return _call_claude_code(system, user, oauth_token, model=model)
        except Exception as exc:  # noqa: BLE001 — provider fallback
            logger.warning(f"Claude Code provider failed: {exc}")
            failures.append(f"claude-code: {_short(exc)}")

    if anthropic_key:
        try:
            return _call_anthropic(system, user, anthropic_key, model=model)
        except Exception as exc:  # noqa: BLE001 — provider fallback
            logger.warning(f"Anthropic provider failed: {exc}")
            failures.append(f"anthropic: {_short(exc)}")

    if openai_key:
        try:
            return _call_openai(system, user, openai_key)
        except Exception as exc:  # noqa: BLE001 — provider fallback
            logger.warning(f"OpenAI provider failed: {exc}")
            failures.append(f"openai: {_short(exc)}")

    return _abstain(failures)


def _short(exc: BaseException, limit: int = 160) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


_AIRE_TOKEN_ENV = "AIRE_BAIR_TOKEN"  # BAIR's own consumer slot on AIRE's door, revocable alone
_AIRE_GATE_DEFAULT = "https://gate.bernarduriza.com"
_AIRE_TIMEOUT_S = 600.0


def _aire_casita(system: str) -> str:
    """``bair-gatekeep-{sha256(prompt)}``. AIRE's door installs a system prompt per
    CASITA (``/init``), not per turn, so the casita is named after the prompt: two
    different prompts — the eval suite A/Bs them in parallel — can never share one
    surface and answer under each other's instructions. Same pattern as
    server-bot's judge (``judge_casita_for``); bounded by the number of prompt
    versions, not by the number of reviews."""
    return f"bair-gatekeep-{hashlib.sha256(system.encode('utf-8')).hexdigest()[:32]}"


def _call_aire(system: str, user: str, token: str, model: str | None = None) -> GatekeepDecision:
    """One review through AIRE with its canonical client (``fi_runner.AIREBackend``):
    mode ``complete`` (the raw-API substitute — no builtins, no agentic loop), no
    MCP servers, no tools, a throwaway session per review. AIRE holds the model
    credential and mirrors the transcript; bair presents only its own token."""
    import asyncio

    from fi_runner import AIREBackend
    from fi_runner.backend import ToolPolicy

    backend = AIREBackend(
        project=_aire_casita(system),
        gate_url=os.environ.get("AIRE_GATE_URL", _AIRE_GATE_DEFAULT),
        auth_token=token,
        default_mode="complete",
        timeout=_AIRE_TIMEOUT_S,
    )
    result = asyncio.run(
        backend.run_turn(
            system_prompt=system,
            user_message=user,
            mcp_servers=[],
            tool_policy=ToolPolicy(),
            model=_claude_model(model),
            session_id=None,
        )
    )
    return _normalize(_extract_json(result.text or ""), provider="aire")


_CLI_TIMEOUT_S = 600  # the binary retries the API itself (429/5xx) inside this budget
_CLI_PROMPT = (
    "Review the pull request given as input. Answer with the JSON object your "
    "instructions require, and nothing else."
)
# The only variables the binary sees: it must not reach the job's GitHub token or
# any other secret, and it must not pick an API key over the subscription.
_CLI_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "NO_PROXY")


def _call_claude_code(system: str, user: str, token: str, model: str | None = None) -> GatekeepDecision:
    """Review through the unmodified `claude` binary in print mode with the
    owner's subscription token (``CLAUDE_CODE_OAUTH_TOKEN``).

    Why the binary and not the Messages API: Anthropic's terms reserve
    subscription OAuth for "ordinary use of Claude Code and other native
    Anthropic applications" and ask developers building tools to use API keys
    (code.claude.com/docs/en/legal-and-compliance). Until 2026-09-26 bair sent
    the token straight to /v1/messages under a spoofed Claude Code identity; the
    binary is the sanctioned path, and the one Anthropic documents for CI.

    Isolation — the binary loads hooks, settings and MCP servers from its working
    directory and HOME, and a PR controls the checkout. So it runs in an empty
    temporary directory with an empty HOME, a minimal environment (no GH_TOKEN,
    no API key), every tool disabled, one turn, and no session saved. The PR
    reaches it only as stdin text."""
    import shutil
    import subprocess as sp
    import tempfile

    binary = shutil.which(os.environ.get("BAIR_CLAUDE_BIN", "claude"))
    if not binary:
        raise RuntimeError("claude CLI not found — the workflow must install Claude Code (claude.ai/install.sh)")
    with tempfile.TemporaryDirectory(prefix="bair-claude-") as tmp:
        system_file = os.path.join(tmp, "gatekeep_system.txt")
        with open(system_file, "w", encoding="utf-8") as fh:
            fh.write(system)
        env = {k: os.environ[k] for k in _CLI_ENV_PASSTHROUGH if k in os.environ}
        env.update(
            HOME=tmp,
            CLAUDE_CONFIG_DIR=os.path.join(tmp, ".claude"),
            CLAUDE_CODE_OAUTH_TOKEN=token,
            DISABLE_AUTOUPDATER="1",
        )
        proc = sp.run(
            [
                binary, "-p", _CLI_PROMPT,
                "--output-format", "json",
                "--system-prompt-file", system_file,
                "--disallowedTools", "*",
                "--max-turns", "1",
                "--no-session-persistence",
                "--model", _claude_model(model),
            ],
            input=user, cwd=tmp, env=env, capture_output=True, text=True, timeout=_CLI_TIMEOUT_S,
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise RuntimeError(f"claude CLI exit {proc.returncode}, unreadable output: {detail}") from exc
    if data.get("is_error") or data.get("subtype") != "success":
        raise RuntimeError(
            f"claude CLI {data.get('subtype')} (api status {data.get('api_error_status')}): "
            f"{str(data.get('result', ''))[:300]}"
        )
    return _normalize(_extract_json(str(data.get("result", ""))), provider="claude-code")


def _call_anthropic(system: str, user: str, key: str, model: str | None = None) -> GatekeepDecision:
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
            "model": _claude_model(model),
            "max_tokens": 4000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Anthropic HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    text = _response_text(data)
    return _normalize(_extract_json(text), provider="anthropic")


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
    return _normalize(_extract_json(text), provider="openai")


def _response_text(data: dict[str, Any]) -> str:
    """The text of a Messages API response: every ``text`` block joined. Models
    with thinking always on (Claude Opus 5.5) put ``thinking`` blocks first, so
    ``content[0]["text"]`` raised KeyError and the gate abstained (eval run
    36209279458: 72/72 opus-5-5 reviews UNAVAILABLE)."""
    text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
    if not text.strip():
        raise ValueError(f"no text block in response (stop_reason={data.get('stop_reason')!r})")
    return text


_VERDICTS = ("APPROVE", "WARN", "BLOCK")
_SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def _normalize(parsed: dict[str, Any], *, provider: str) -> GatekeepDecision:
    """The model's JSON, made safe for the code that gates on it. Before this, a
    verdict like "block" or "REQUEST_CHANGES" skipped the severity floor and exited
    0 (a BLOCK merged silently), and a null severity/issues crashed the gate outside
    the provider fallback — a red check with no comment. Unknown severities read as
    MEDIUM; a non-dict issue is kept as an unstructured MEDIUM finding; an unknown
    verdict is derived from the worst severity and is never milder than WARN."""
    severity = str(parsed.get("severity") or "").strip().upper()
    if severity not in _SEVERITIES:
        severity = "MEDIUM"
    raw_issues = parsed.get("issues")
    issues: list[dict[str, str]] = []
    for item in raw_issues if isinstance(raw_issues, list) else []:
        if isinstance(item, dict):
            issues.append({str(k): v if isinstance(v, str) or v is None else str(v) for k, v in item.items()})
        elif item is not None:
            issues.append({"type": "unstructured", "severity": "MEDIUM", "rule": "general", "message": str(item)})
    verdict = str(parsed.get("verdict") or "").strip().upper()
    if verdict not in _VERDICTS:
        worst = [severity, *(str(i.get("severity") or "").upper() for i in issues)]
        verdict = "BLOCK" if "CRITICAL" in worst else "WARN"
        logger.warning(f"gatekeep: model returned verdict {parsed.get('verdict')!r}; read as {verdict}")
    return GatekeepDecision(
        verdict=verdict,
        severity=severity,
        summary=str(parsed.get("summary") or ""),
        issues=issues,
        recommendation=str(parsed.get("recommendation") or ""),
        provider=provider,
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Lenient JSON parse — strips ```json fences```, trims whitespace, and reads
    the first JSON object, ignoring prose before or after it (1 of 72 opus-4-7
    eval reviews failed with ``Extra data`` on trailing text)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # ```json ... ``` or ``` ... ```
        lines = stripped.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines)
    start = stripped.find("{")
    if start < 0:
        raise ValueError("no JSON object in model output")
    obj, _ = json.JSONDecoder().raw_decode(stripped, start)
    if not isinstance(obj, dict):
        raise ValueError("model output JSON is not an object")
    return obj


# -- Emit (comment + GITHUB_OUTPUT + exit code) ------------------------

_VERDICT_HEADERS = {
    "APPROVE":     "## :white_check_mark: BAIR Gatekeeper: APPROVED",
    "WARN":        "## :warning: BAIR Gatekeeper: WARNING",
    "BLOCK":       "## :x: BAIR Gatekeeper: BLOCKED",
    "UNAVAILABLE": "## :zzz: BAIR Gatekeeper: ABSTAINED (no LLM provider — not blocking)",
}


def _render_comment(d: GatekeepDecision) -> str:
    """Markdown body for the PR comment."""
    header = _VERDICT_HEADERS.get(d.verdict, "## BAIR Gatekeeper")
    parts = [header, "", f"**Severity:** {d.severity}  ", f"**Provider:** `{d.provider}`", "", d.summary]
    if d.would_block:
        parts.append(
            "\n> **Would block (shadow mode).** The data-plane rules report here but do not gate "
            "the merge until BAIR's eval suite passes on held-out cases (BernardUriza/.github "
            "backlog 01). Treat the CRITICAL findings below as a real stop sign."
        )
    if d.issues:
        parts.append("\n### Issues\n")
        for i in d.issues:
            sev = i.get("severity", "?")
            typ = i.get("type", "?")
            msg = i.get("message", "")
            parts.append(f"- **[{sev}] {typ}** — {msg}")
    if d.recommendation:
        parts.append(f"\n### Recommendation\n{d.recommendation}")
    parts.append(
        "\n---\n*Posted by BAIR Gatekeeper. Only a BLOCK verdict gates the merge; "
        "when no LLM provider answers it abstains out loud instead of failing closed or approving in silence.*"
    )
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


def review(
    diff: str, root: str, repo: str, pr_num: str, context_mode: str | None = None, model: str | None = None
) -> GatekeepDecision:
    """The verdict on ``diff`` with the target repo checked out at ``root``: rules,
    changed-code context, LLM, severity floor, shadow. Shared by the live gate and
    the eval suite (``bair.evals``) so both measure the same path."""
    if not diff:
        logger.warning("empty diff; nothing to review — APPROVE by default")
        return GatekeepDecision(
            verdict="APPROVE", severity="LOW",
            summary="Empty diff; no code changes to review.",
            issues=[], recommendation="", provider="none",
        )
    # Read the TARGET repo's own doctrine (checked out at the root) so the
    # gatekeeper reviews against project rules, not just generic smell. Empty
    # when the repo ships no .claude rules → a generic review, never an error.
    repo_rules = gather_repo_rules(root)
    if repo_rules:
        logger.info(f"gatekeep: loaded {len(repo_rules)} bytes of repository rules")
    else:
        logger.info("gatekeep: no .claude repository rules found — generic review")
    playbook_rules = gather_playbook_rules()
    if playbook_rules:
        logger.info(f"gatekeep: loaded {len(playbook_rules)} bytes of universal playbook rules")
    else:
        logger.info("gatekeep: no universal playbook rules reachable — repo-only review")
    code_context = gather_changed_context(diff, root, mode=context_mode)
    logger.info(f"gatekeep: loaded {len(code_context)} bytes of changed-code context")
    user_msg = _build_user_msg(diff, repo_rules, repo, pr_num, playbook_rules, code_context)
    return _shadow(_floor_verdict(_call_llm(load_prompt("gatekeep_system"), user_msg, model=model)))


@command("gatekeep")
def gatekeep(ctx: CommandContext, container: Container) -> None:
    """The gatekeep pipeline. Called by ai-gatekeep.yml on every PR.

    Reads PR_NUM, REPO, BASE_SHA, HEAD_SHA from env (or ctx for the
    first three). Fetches the diff, runs the LLM, posts a comment,
    writes ``verdict`` to $GITHUB_OUTPUT, and exits non-zero only on BLOCK.
    UNAVAILABLE (no provider) abstains: comment + ``abstained=true``, exit 0."""
    repo = ctx.repo or os.environ.get("REPO", "")
    pr_num = ctx.pr_num or os.environ.get("PR_NUM", "")
    base_sha = os.environ.get("BASE_SHA", "")
    head_sha = os.environ.get("HEAD_SHA", "")

    if not (repo and pr_num and base_sha and head_sha):
        logger.error(f"missing env: REPO={repo!r} PR_NUM={pr_num!r} BASE_SHA={base_sha!r} HEAD_SHA={head_sha!r}")
        _set_output("verdict", "UNAVAILABLE")
        _set_output("executed", "false")
        sys.exit(1)

    decision = review(_get_diff(base_sha, head_sha), ".", repo, pr_num)

    body = _render_comment(decision)
    _post_comment(container, repo, pr_num, body)

    _set_output("verdict", decision.verdict)
    _set_output("severity", decision.severity)
    _set_output("provider", decision.provider)
    _set_output("executed", "true")
    _set_output("abstained", "true" if decision.verdict == "UNAVAILABLE" else "false")
    _set_output("would_block", "true" if decision.would_block else "false")

    logger.info(f"BAIR gatekeep verdict={decision.verdict} severity={decision.severity} provider={decision.provider}")

    code = _exit_code(decision.verdict)
    if code:
        sys.exit(code)


_VERDICT_RANK = {"APPROVE": 0, "WARN": 1, "BLOCK": 2}
_SEVERITY_FLOOR = {"HIGH": "WARN", "CRITICAL": "BLOCK"}


def _floor_verdict(d: GatekeepDecision) -> GatekeepDecision:
    """The verdict never sits below its most severe issue: a CRITICAL reported
    under WARN would merge. UNAVAILABLE is an abstention and is left alone."""
    if d.verdict not in _VERDICT_RANK:
        return d
    severities = [d.severity, *(str(i.get("severity", "")) for i in d.issues)]
    floor = max((_SEVERITY_FLOOR.get(s.upper(), "APPROVE") for s in severities), key=_VERDICT_RANK.__getitem__)
    if _VERDICT_RANK[floor] <= _VERDICT_RANK[d.verdict]:
        return d
    logger.warning(f"gatekeep: verdict {d.verdict} raised to {floor} to match the reported severity")
    return replace(d, verdict=floor)


# Rules that report but do not gate yet: they run in shadow until the eval suite
# (backlog 01) clears them. Emptying this set is the whole promotion step.
# Attribution reads the enumerated `rule` field the prompt's schema requires; the
# free-text `type` is only a fallback — the 2026-09-26 baseline showed the model
# does not reliably type data-plane issues as `data_plane`.
_SHADOW_RULES = frozenset({"data_plane"})


def _in_shadow(issue: dict) -> bool:
    rule = str(issue.get("rule") or "").strip().lower()
    return rule in _SHADOW_RULES or (not rule and str(issue.get("type", "")) in _SHADOW_RULES)


def _shadow(d: GatekeepDecision) -> GatekeepDecision:
    """A BLOCK that rests only on shadow-mode issues exits as WARN and says it
    would have blocked. Any CRITICAL outside the shadow set still blocks, and a
    BLOCK with no issues to attribute it to is left alone."""
    if d.verdict != "BLOCK" or not d.issues:
        return d
    critical = [i for i in d.issues if str(i.get("severity", "")).upper() == "CRITICAL"]
    if not critical or not all(_in_shadow(i) for i in critical):
        return d
    logger.warning("gatekeep: BLOCK held back to WARN — only shadow-mode (data_plane) issues are CRITICAL")
    return replace(d, verdict="WARN", would_block=True)


def _exit_code(verdict: str) -> int:
    """Only BLOCK gates the merge. WARN passes with the concern in the comment;
    UNAVAILABLE is an abstention, and a reviewer with no model has no veto."""
    return 1 if verdict == "BLOCK" else 0


# Optional ack appearance when the command is triggered via PR comment.
# In CI-triggered mode (the typical path) ack is moot — the comment is
# the deliverable.
register_ack_meta("gatekeep", icon="🛡️", label="Gatekeeper")

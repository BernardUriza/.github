"""Auto-deep heuristic — decides whether a PR warrants Claude deep analysis.

Pure function. No I/O. Uses only data already gathered (diff text + file count).
Returns (should_deep, reasons) so the decision is logged and auditable.
"""

from __future__ import annotations

import re

# ── Thresholds ───────────────────────────────────────────────────

_MIN_DIFF_LINES = 150       # PRs under this are simple enough for GPT alone
_LARGE_DIFF_LINES = 500     # PRs above this always benefit from deep analysis
_LARGE_FILE_COUNT = 8       # Many files = high blast radius

# ── Complexity signals (regex patterns on diff text) ─────────────

_HIGH_COMPLEXITY_PATTERNS: list[tuple[str, str]] = [
    (r"\.guard\.|Guard|@UseGuards|canActivate", "auth/guard logic"),
    (r"sse|SSE|EventSource|streamBotResponse|startSSEHeartbeat", "SSE/streaming"),
    (r"organisationId|organizationId|orgId|x-org-id|OrgScoped", "multi-tenant/org-scoping"),
    (r"\.error\(|logger\.error|console\.error|Sentry\.", "error handling/logging"),
    (r"presignedUrl|getSignedUrl|S3|putObject", "S3/presigned URL"),
    (r"try\s*\{[\s\S]{200,}catch", "large try/catch blocks"),
    (r"OPENAI_API_KEY|ANTHROPIC|apiKey|secret|token", "secret/credential handling"),
    (r"\.pipe\(|interceptor|middleware|ValidationPipe", "NestJS pipeline/middleware"),
    (r"useEffect.*\[.*\]|setInterval|setTimeout|clearInterval", "effect lifecycle"),
    (r"import.*from.*supabase|createClient|auth\.getUser", "auth/Supabase"),
]

# ── CSS/config-only signals (skip deep) ──────────────────────────

_LOW_COMPLEXITY_PATTERNS: list[tuple[str, str]] = [
    (r"^[+-]\s*@apply\s", "CSS @apply only"),
    (r"^[+-]\s*\"version\":", "version bump"),
    (r"\.css$|\.scss$|\.less$", "stylesheet only"),
]


def should_auto_deep(diff: str, changed_files: int) -> tuple[bool, list[str]]:
    """Evaluate whether a PR warrants Claude deep analysis.

    Returns (should_deep, reasons).
    Reasons are human-readable strings logged in the trace.
    """
    reasons: list[str] = []
    diff_lines = len(diff.splitlines())

    # ── Hard thresholds ──────────────────────────────────────────

    # ── Size signals ───────────────────────────────────────────
    if diff_lines >= _LARGE_DIFF_LINES:
        reasons.append(f"large diff ({diff_lines} lines)")

    if changed_files >= _LARGE_FILE_COUNT:
        reasons.append(f"many files ({changed_files})")

    # ── Complexity signal scan ───────────────────────────────────

    for pattern, label in _HIGH_COMPLEXITY_PATTERNS:
        if re.search(pattern, diff):
            reasons.append(label)

    # ── Small PR shortcut (only if zero complexity signals) ─────
    if diff_lines < _MIN_DIFF_LINES and changed_files <= 2 and not reasons:
        return False, [f"small PR ({diff_lines} lines, {changed_files} files)"]

    # ── Low-complexity override ──────────────────────────────────
    # If ALL changed lines match low-complexity patterns, skip deep
    changed_lines = [l for l in diff.splitlines() if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))]
    if changed_lines:
        low_count = sum(
            1 for l in changed_lines
            if any(re.search(p, l) for p, _ in _LOW_COMPLEXITY_PATTERNS)
        )
        if low_count == len(changed_lines):
            return False, ["CSS/config-only changes"]

    # ── Decision ─────────────────────────────────────────────────
    # 2+ complexity signals = deep. 1 signal + large diff = deep.
    if len(reasons) >= 2:
        return True, reasons
    if len(reasons) == 1 and diff_lines >= _LARGE_DIFF_LINES:
        return True, reasons

    return False, reasons or [f"below complexity threshold ({diff_lines} lines, {changed_files} files)"]

"""CSS health gatherer — fetches CSS files and delegates analysis to domain."""

from __future__ import annotations

import base64

from ..domain.css_analyzer import CssFinding, extract_selectors, find_duplicates
from ..domain.invariants import GLOBALS_CSS_CANDIDATES
from ..domain.models import PRIdentifier
from ..infra.constants import CSS_HEALTH
from ..contracts import ActionsIO, FileStore, GitHubClient


def _fetch_file_content(github: GitHubClient, repo: str, path: str, ref: str) -> str:
    """Fetch a file's content from GitHub at a specific ref."""
    raw = github.run_gh(
        "api", f"repos/{repo}/contents/{path}?ref={ref}",
        "--jq", ".content", check=False,
    )
    content_b64 = raw.strip().replace("\n", "")
    if not content_b64:
        return ""
    try:
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _get_css_files_and_sha(
    pr: PRIdentifier, github: GitHubClient,
) -> tuple[list[str], str]:
    """Get changed CSS files and the PR head SHA."""
    raw_files = github.run_gh(
        "api", f"repos/{pr.repo}/pulls/{pr.number}/files?per_page=100",
        "--jq", ".[].filename", check=False,
    )
    changed = [f.strip() for f in raw_files.strip().splitlines() if f.strip()]
    css_files = [f for f in changed if f.endswith(".css")]

    head_sha = github.run_gh(
        "api", f"repos/{pr.repo}/pulls/{pr.number}",
        "--jq", ".head.sha", check=False,
    ).strip()

    return css_files, head_sha


def _format_findings(findings: list[CssFinding]) -> str:
    output = f"Found {len(findings)} CSS health issue(s):\n\n"
    for i, f in enumerate(findings, 1):
        output += f"{i}. {f.format()}\n"
    return output


def gather_css_health(
    pr: PRIdentifier, github: GitHubClient, store: FileStore, actions: ActionsIO,
) -> str:
    """Run CSS duplicate selector analysis. Returns findings text."""
    css_files, head_sha = _get_css_files_and_sha(pr, github)

    if not css_files:
        actions.notice("No CSS files in diff -- skipping CSS health check")
        store.write(CSS_HEALTH, "")
        return ""

    if not head_sha:
        actions.warning("Could not get PR head SHA -- skipping CSS health check")
        store.write(CSS_HEALTH, "")
        return ""

    actions.notice(f"CSS health check: analyzing {len(css_files)} CSS files")

    files_to_fetch = list(css_files)
    for g in GLOBALS_CSS_CANDIDATES:
        if g not in files_to_fetch:
            files_to_fetch.append(g)

    file_selectors: dict[str, list[tuple[str, int]]] = {}
    for path in files_to_fetch:
        content = _fetch_file_content(github, pr.repo, path, head_sha)
        if content:
            sels = extract_selectors(content)
            if sels:
                file_selectors[path] = sels

    findings = find_duplicates(file_selectors)

    if not findings:
        actions.notice("CSS health check: no issues found")
        store.write(CSS_HEALTH, "")
        return ""

    text = _format_findings(findings)
    actions.notice(f"CSS health check: {len(findings)} issues found")
    store.write(CSS_HEALTH, text)
    return text

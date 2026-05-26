"""Scan the codebase for prior art related to new code in the PR diff.

Uses the Git Trees API (1 call, 5000/hr limit) instead of Search API
(N calls, 10/min limit). Matches file NAMES only — content search is
handled by Claude deep analysis when available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..domain.invariants import NOISE_NAMES
from ..infra.constants import PRIOR_ART_CONTEXT, TEMPLATES_DIR
from ..contracts import ActionsIO, FileStore, GitHubClient
from ..log import logger

_TEMPLATE = (TEMPLATES_DIR / "prior_art_header.md").read_text(encoding="utf-8")


# ── Models ───────────────────────────────────────────────────────


@dataclass
class PriorArtMatch:
    name: str
    files: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines = [f"### `{self.name}` — found in {len(self.files)} existing files (not in this PR)"]
        lines.extend(f"  - `{f}`" for f in self.files[:6])
        if len(self.files) > 6:
            lines.append(f"  - ... and {len(self.files) - 6} more")
        return "\n".join(lines)


# ── Extract ──────────────────────────────────────────────────────


def _extract_names(diff: str) -> tuple[set[str], set[str]]:
    """Extract exported names + component filenames from diff. Returns (names, changed_files)."""
    added_lines = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    added_text = "\n".join(added_lines)

    name_pattern = r"\b(?:export\s+)?(?:function|const|let|type|interface|enum|class)\s+([A-Z][A-Za-z0-9_]+)"
    code_names = set(re.findall(name_pattern, added_text))

    new_files = re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)
    file_components = {m.group(1) for f in new_files if (m := re.search(r"([A-Z][A-Za-z0-9]+)\.tsx$", f))}

    all_names = (code_names | file_components) - NOISE_NAMES
    return all_names, set(new_files)


# ── Search (Git Trees — 1 API call) ─────────────────────────────


def _fetch_file_tree(repo: str, github: GitHubClient) -> list[str]:
    """Fetch all file paths via Git Trees API. ONE call, no rate limit risk."""
    try:
        raw = github.run_gh(
            "api", f"repos/{repo}/git/trees/HEAD?recursive=1",
            "--jq", ".tree[].path",
            check=False,
        )
    except Exception as e:
        logger.warning(f"  [prior-art] git/trees failed: {e}")
        return []

    if not raw.strip() or "API rate limit" in raw:
        return []

    return [line for line in raw.strip().splitlines() if line]


def _match_names_in_tree(
    names: set[str], tree: list[str], changed_files: set[str],
) -> list[PriorArtMatch]:
    """Match symbol names against file paths in the tree. Pure, zero I/O."""
    # Build a lowercase lookup for case-insensitive matching
    matches = []
    for name in sorted(names):
        name_lower = name.lower()
        hits = []
        for path in tree:
            # Skip files that are part of the PR diff
            if path in changed_files:
                continue
            # Match: filename stem contains the symbol name (case-insensitive)
            stem = PurePosixPath(path).stem.lower()
            if name_lower == stem or name_lower in stem.replace("-", "").replace("_", ""):
                hits.append(path)
        if hits:
            matches.append(PriorArtMatch(name=name, files=hits))

    return matches


# ── Format ───────────────────────────────────────────────────────


def _format_matches(matches: list[PriorArtMatch], total_names: int) -> str:
    """Render matches into template, or a 'no prior art' message."""
    if not matches:
        return f"No prior art found — all {total_names} names appear to be new to the codebase.\n"
    items = "\n\n".join(m.format() for m in matches)
    return _TEMPLATE.format(matches=items)


# ── Public API ───────────────────────────────────────────────────


def gather_prior_art(
    repo: str, diff: str, github: GitHubClient, store: FileStore, actions: ActionsIO,
) -> str:
    """Find existing files matching names introduced in the diff.

    Uses Git Trees API (1 call) for filename matching. Content-level
    prior art detection is handled by Claude deep analysis.
    """
    if not diff.strip():
        actions.notice("No diff found — skipping prior art scan")
        store.write(PRIOR_ART_CONTEXT, "")
        return ""

    names, changed_files = _extract_names(diff)

    if not names:
        actions.notice("No new exported names found in diff — skipping prior art scan")
        store.write(PRIOR_ART_CONTEXT, "")
        return ""

    actions.notice(f"Prior art scan: matching {len(names)} names against file tree (1 API call)")
    tree = _fetch_file_tree(repo, github)

    if not tree:
        actions.warning("Prior art scan: could not fetch file tree — skipping")
        store.write(PRIOR_ART_CONTEXT, "")
        return ""

    logger.info(f"  [prior-art] tree: {len(tree)} files, matching {len(names)} names")
    matches = _match_names_in_tree(names, tree, changed_files)
    text = _format_matches(matches, len(names))

    store.write(PRIOR_ART_CONTEXT, text)
    actions.notice(f"Prior art scan: {len(matches)}/{len(names)} names have existing files")
    return text

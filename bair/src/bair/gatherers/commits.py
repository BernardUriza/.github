"""Gather commits from git log between two refs."""

from __future__ import annotations

from ..config import ChangelogConfig
from ..domain.models import CommitEntry
from ..contracts import GitHubClient


def gather_commits(github: GitHubClient, cfg: ChangelogConfig) -> list[CommitEntry]:
    """Fetch commit log between base and head refs. Returns typed entries."""
    # --first-parent: one entry per PR (the merge commit on a merge-strategy repo,
    # the squash commit on a squash-strategy repo). The trailing #NNNN survives in
    # both forms, which CommitEntry.from_log_line extracts. Earlier behavior
    # (--no-merges) returned individual feature commits with no PR# on
    # merge-strategy repos like xair-org-gen-backend, leaving the LLM to guess
    # the PR linkage.
    git_args = [
        "log",
        f"{cfg.base_ref}..{cfg.head_ref}",
        "--first-parent",
        "--format=%H|%s|%an|%ad",
        "--date=short",
    ]
    if cfg.since_date:
        git_args.append(f"--since={cfg.since_date}")
    if cfg.until_date:
        git_args.append(f"--until={cfg.until_date}")

    raw = github.run_git(*git_args, check=False).strip()
    if not raw:
        return []
    return [CommitEntry.from_log_line(line) for line in raw.splitlines()]

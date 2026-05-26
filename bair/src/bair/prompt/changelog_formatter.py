"""Format commit entries into the changelog LLM user message."""

from __future__ import annotations

from datetime import date

from ..config import ChangelogConfig
from ..infra.constants import TEMPLATES_DIR
from ..domain.models import CommitEntry

_TEMPLATE = (TEMPLATES_DIR / "changelog_user_message.md").read_text(encoding="utf-8")


def format_commit_input(commits: list[CommitEntry], cfg: ChangelogConfig) -> str:
    """Build the user message for the LLM from typed commit entries."""
    return _TEMPLATE.format(
        commit_count=len(commits),
        repo=cfg.repo,
        base_ref=cfg.base_ref,
        head_ref=cfg.head_ref,
        today=date.today().isoformat(),
        commits="\n".join(c.format_line() for c in commits),
    )

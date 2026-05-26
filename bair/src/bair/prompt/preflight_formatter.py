"""Format commit entries into the preflight LLM user message."""

from __future__ import annotations

from datetime import date

from ..config import PreflightConfig
from ..infra.constants import TEMPLATES_DIR
from ..domain.models import CommitEntry

_TEMPLATE = (TEMPLATES_DIR / "preflight_user_message.md").read_text(encoding="utf-8")


def format_preflight_input(commits: list[CommitEntry], cfg: PreflightConfig) -> str:
    """Build the user message for the preflight LLM from typed commit entries."""
    return _TEMPLATE.format(
        commit_count=len(commits),
        repo=cfg.repo,
        base_ref=cfg.base_ref,
        head_ref=cfg.head_ref or "<HEAD>",
        target_service=cfg.target_service,
        target_time=cfg.target_time or "(time TBD)",
        today=date.today().isoformat(),
        commits="\n".join(c.format_line() for c in commits),
    )

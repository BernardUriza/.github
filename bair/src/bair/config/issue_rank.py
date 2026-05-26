"""Configuration for the issue-rank pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..infra.constants import require_env


@dataclass(frozen=True, slots=True)
class IssueRankConfig:
    """Issue ranking pipeline configuration."""
    team_key: str = "VIS"
    top_n: int = 10           # Number of top issues to return
    slack_channel: str = ""   # Optional: post results to Slack
    dry_run: bool = False

    @classmethod
    def from_env(cls, dry_run: bool = False) -> IssueRankConfig:
        return cls(
            team_key=os.environ.get("LINEAR_TEAM_KEY", "VIS"),
            top_n=int(os.environ.get("ISSUE_RANK_TOP_N", "10")),
            slack_channel=os.environ.get("SLACK_CHANNEL", ""),
            dry_run=dry_run,
        )

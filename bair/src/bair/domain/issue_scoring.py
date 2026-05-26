"""Issue scoring — pure ranking logic, no I/O.

Computes a composite score for each issue based on:
  - Priority (from Linear: 1=urgent → 4=low)
  - Project importance (Core 2.0 Launch > Sprint > others)
  - Readiness (GPT assessment: does the spec have enough detail?)
  - Feasibility (Agent SDK: do referenced files exist in the repo?)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math


# ── Weights ──────────────────────────────────────────────────────

W_PRIORITY = 3.0
W_PROJECT = 2.0
W_READINESS = 2.0
W_FEASIBILITY = 1.0
W_STALENESS = 1.0


# ── Score Maps ───────────────────────────────────────────────────

# Linear priority: 1=urgent, 2=high, 3=medium, 4=low, 0=none
_PRIORITY_SCORE = {1: 1.0, 2: 0.75, 3: 0.5, 4: 0.25, 0: 0.1}

_PROJECT_SCORE = {
    "Core 2.0 Launch": 1.0,
    "Core 2.0 Stabilization": 0.9,
    "Apollo": 0.8,
    "Integrations": 0.7,
}
_PROJECT_DEFAULT = 0.3


@dataclass(slots=True)
class ScoredIssue:
    """An issue with computed ranking scores."""
    identifier: str
    title: str
    description: str
    priority: int
    priority_label: str
    state_name: str
    project_name: str
    assignee_name: str
    labels: list[str]
    git_branch_name: str

    # Scores (0.0–1.0)
    score_priority: float = 0.0
    score_project: float = 0.0
    score_readiness: float = 0.0     # Filled by GPT
    score_feasibility: float = 0.0   # Filled by Agent SDK
    score_staleness: float = 0.0
    readiness_reason: str = ""
    feasibility_reason: str = ""

    # Computed
    total_score: float = 0.0
    rank: int = 0
    created_at: str = ""

    def compute_total(self) -> None:
        self.total_score = (
            self.score_priority * W_PRIORITY +
            self.score_project * W_PROJECT +
            self.score_readiness * W_READINESS +
            self.score_feasibility * W_FEASIBILITY +
            self.score_staleness * W_STALENESS
        )


def score_priority(priority: int) -> float:
    return _PRIORITY_SCORE.get(priority, 0.1)


def score_project(project_name: str) -> float:
    return _PROJECT_SCORE.get(project_name, _PROJECT_DEFAULT)


def score_staleness(created_at: str) -> float:
    """Older issues get higher staleness score (log scale, capped at 1.0)."""
    if not created_at:
        return 0.0
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - created).days
        return min(1.0, math.log2(max(days, 1)) / 8.0)  # 256 days = 1.0
    except Exception:
        return 0.0


def rank_issues(scored: list[ScoredIssue]) -> list[ScoredIssue]:
    """Compute totals and assign ranks. Mutates in place, returns sorted list."""
    for s in scored:
        s.compute_total()
    scored.sort(key=lambda s: s.total_score, reverse=True)
    for i, s in enumerate(scored):
        s.rank = i + 1
    return scored

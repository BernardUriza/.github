"""Config for the resolve pipeline — reads from env vars automatically."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class ResolveConfig(BaseSettings):
    """Config for the autonomous resolve pipeline. Reads env vars automatically."""

    issue_id: str = Field(alias="ISSUE_ID")
    repo: str = Field(alias="REPO")
    base_branch: str = Field(default="", alias="BASE_BRANCH")
    max_agent_turns: int = Field(default=100, alias="MAX_AGENT_TURNS")
    dry_run: bool = False

    model_config = {"populate_by_name": True}

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        # Auto-detect base branch from repo name if not explicitly set
        if not self.base_branch:
            object.__setattr__(
                self,
                "base_branch",
                "staging-v2" if "backend" in self.repo else "main",
            )

    @classmethod
    def from_env(cls, **overrides: object) -> ResolveConfig:
        return cls(**overrides)

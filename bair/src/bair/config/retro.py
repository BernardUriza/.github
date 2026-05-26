"""Config for the retro (post-review analysis) pipeline — reads from env vars automatically."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from ..infra.constants import OPENAI_MODEL


class RetroConfig(BaseSettings):
    """Config for the AI Retro pipeline. Reads env vars automatically."""

    pr_num: str = Field(default="", alias="PR_NUM")
    repo: str = Field(default="", alias="REPO")
    variant: str = Field(default="frontend", alias="PROMPT_VARIANT")
    # SSOT: bair/infra/constants.py:OPENAI_MODEL. No env override.
    model: str = Field(default=OPENAI_MODEL, frozen=True)
    run_id: str = Field(default="local", alias="GITHUB_RUN_ID")
    # Populated from GUIDANCE env var (workflow_dispatch) or passed explicitly.
    guidance: str = Field(default="", alias="GUIDANCE")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_env(cls, *, guidance: str = "") -> RetroConfig:
        # pydantic-settings reads all aliased fields from env vars automatically.
        # We only pass guidance explicitly when the caller wants to override
        # the GUIDANCE env var (e.g. from a programmatic test harness).
        return cls(guidance=guidance) if guidance else cls()

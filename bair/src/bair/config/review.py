"""Config for the full review pipeline — reads from env vars automatically."""

from __future__ import annotations

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings

from ..infra.constants import (
    DEFAULT_MAX_DIFF_BYTES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    OPENAI_MODEL,
)


class DeepMode(Enum):
    """Deep analysis mode — auto, forced, or disabled."""
    AUTO = "auto"
    FORCED = "forced"
    DISABLED = "disabled"


class ReviewConfig(BaseSettings):
    """Config for the full review pipeline. Reads env vars automatically."""

    pr_num: str = Field(alias="PR_NUM")
    repo: str = Field(alias="REPO")
    prompt_file: str = Field(alias="PROMPT_FILE")
    # SSOT: bair/infra/constants.py:OPENAI_MODEL. No env override.
    model: str = Field(default=OPENAI_MODEL, frozen=True)
    max_diff_bytes: int = Field(default=DEFAULT_MAX_DIFF_BYTES, alias="MAX_DIFF_BYTES")
    variant: str = Field(default="", alias="PROMPT_VARIANT")
    run_id: str = Field(default="local-dry-run", alias="GITHUB_RUN_ID")
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, alias="MAX_TOKENS")
    temperature: float = Field(default=DEFAULT_TEMPERATURE, alias="TEMPERATURE")
    dry_run: bool = False
    deep_mode: DeepMode = DeepMode.AUTO
    skip_dedup: bool = False

    model_config = {"populate_by_name": True}

    @classmethod
    def from_env(
        cls,
        *,
        dry_run: bool = False,
        deep_mode: DeepMode = DeepMode.AUTO,
        skip_dedup: bool = False,
    ) -> ReviewConfig:
        return cls(dry_run=dry_run, deep_mode=deep_mode, skip_dedup=skip_dedup)

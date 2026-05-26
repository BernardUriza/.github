"""Config for the changelog pipeline — reads from env vars automatically."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from ..infra.constants import OPENAI_MODEL


class ChangelogConfig(BaseSettings):
    """Config for the changelog pipeline. Reads env vars automatically."""

    base_ref: str = Field(alias="BASE_REF")
    head_ref: str = Field(default="", alias="HEAD_REF")
    repo: str = Field(alias="REPO")
    # SSOT: bair/infra/constants.py:OPENAI_MODEL. No env override.
    model: str = Field(default=OPENAI_MODEL, frozen=True)
    slack_webhook: str = Field(default="", alias="SLACK_WEBHOOK_URL")
    prompt_file: str = Field(default="/tmp/changelog-prompt.md", alias="PROMPT_FILE")
    since_date: str = Field(default="", alias="SINCE_DATE")
    until_date: str = Field(default="", alias="UNTIL_DATE")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_env(cls) -> ChangelogConfig:
        return cls()

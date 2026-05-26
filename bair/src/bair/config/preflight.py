"""Config for the preflight (pre-deploy announcement) pipeline.

Reads env vars automatically. Distinct from ChangelogConfig because the
preflight prompt has different output expectations (customer-facing
impact one-liner + risk flags) and the Slack post format is Block Kit
JSON, not plain text.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings

from ..infra.constants import OPENAI_MODEL


class PreflightConfig(BaseSettings):
    """Config for the preflight pipeline. Reads env vars automatically."""

    # Commit range
    base_ref: str = Field(alias="BASE_REF")
    head_ref: str = Field(default="", alias="HEAD_REF")
    repo: str = Field(alias="REPO")

    # Deploy metadata (what the LLM uses to compose the announcement)
    target_service: str = Field(alias="TARGET_SERVICE")
    target_time: str = Field(default="", alias="TARGET_TIME")  # ISO-8601 or human "in ~2 hours"

    # LLM config — single source of truth lives in bair/infra/constants.py
    model: str = Field(default=OPENAI_MODEL, frozen=True)

    # Slack delivery
    slack_webhook: str = Field(default="", alias="SLACK_WEBHOOK_URL")
    slack_channel_hint: str = Field(default="", alias="SLACK_CHANNEL_HINT")  # for context block

    # Filesystem
    prompt_file: str = Field(default="/tmp/preflight-prompt.md", alias="PROMPT_FILE")

    model_config = {"populate_by_name": True}

    @classmethod
    def from_env(cls) -> PreflightConfig:
        return cls()

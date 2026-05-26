"""Local dry-run service -- resolves secrets, sets env, writes logs.

Provides CLI-callable entry points that:
1. Auto-resolve secrets (OPENAI_API_KEY, ANTHROPIC_API_KEY, GH_TOKEN)
2. Manage environment variables with automatic restoration
3. Run the full pipeline in dry-run mode
4. Tee all output to a timestamped log file in logs/
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Generator

from ..domain.exceptions import ConfigError
from ..infra.constants import OPENAI_MODEL

logger = logging.getLogger(__name__)


# -- Default paths ----------------------------------------------------

# local_runner.py -> services/ -> xair/ -> scripts/ -> .github/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
_DEFAULT_LOGS_DIR = _REPO_ROOT / "logs"
_DEFAULT_STAGING_ENV = Path.home() / ".secrets" / "xair-org-backend-staging.env"
_DEFAULT_ANTHROPIC_KEY_FILE = Path.home() / ".secrets" / "anthropic-api-key.txt"


# -- TeeWriter (stream multiplexer) ----------------------------------

class TeeWriter:
    """Stream multiplexer -- writes to both a log file and the console.

    The log file receives full UTF-8; the console gets a safe ASCII
    fallback when the terminal encoding can't handle certain characters.
    """

    def __init__(self, log_file: IO[str], console: IO[str]) -> None:
        self._file = log_file
        self._console = console

    def write(self, data: str) -> int:
        self._file.write(data)
        try:
            self._console.write(data)
        except UnicodeEncodeError:
            self._console.write(
                data.encode("ascii", errors="replace").decode("ascii")
            )
        return len(data)

    def flush(self) -> None:
        self._console.flush()
        self._file.flush()

    def close(self) -> None:
        self._file.close()


# -- Context managers -------------------------------------------------

@contextmanager
def _env_override(overrides: dict[str, str]) -> Generator[None, None, None]:
    """Temporarily set environment variables, restoring originals on exit."""
    originals: dict[str, str | None] = {}
    for key, value in overrides.items():
        originals[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, original in originals.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


@contextmanager
def _pipeline_context(log_path: Path) -> Generator[None, None, None]:
    """Set up stdout tee and logging for a local pipeline run.

    Stdout is redirected through TeeWriter so that both the pipeline's
    ``print()`` output and this module's ``logger`` output end up in
    the log file AND the console simultaneously.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    console = sys.__stdout__ or sys.stderr
    tee = TeeWriter(log_file=fh, console=console)
    sys.stdout = tee  # type: ignore[assignment]

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    try:
        yield
    finally:
        logger.removeHandler(handler)
        sys.stdout = sys.__stdout__
        tee.close()
        logger.info(f"\nFull log: {log_path}")


# -- Secret resolution ------------------------------------------------

def _read_dotenv_value(file_path: Path, key: str) -> str:
    """Read a single value from a dotenv-style file.

    Uses ``python-dotenv`` when available; falls back to a simple
    KEY=VALUE line parser that handles comments and quoted values.
    """
    if not file_path.exists():
        return ""

    try:
        from dotenv import dotenv_values  # type: ignore[import-untyped]

        values = dotenv_values(file_path)
        return (values.get(key) or "").strip()
    except ImportError:
        for line in file_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, _, v = stripped.partition("=")
            if k.strip() == key:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                return v
        return ""


def _resolve_secret(
    env_name: str,
    *,
    dotenv_file: Path | None = None,
    command: list[str] | None = None,
) -> str:
    """Resolve a secret via a priority chain: env -> dotenv file -> shell command."""
    value = os.environ.get(env_name, "")
    if value:
        return value

    if dotenv_file is not None:
        value = _read_dotenv_value(dotenv_file, env_name)
        if value:
            return value

    if command is not None:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return result.stdout.strip()
        except (
            subprocess.CalledProcessError,
            FileNotFoundError,
            subprocess.TimeoutExpired,
        ):
            pass

    return ""


@dataclass(frozen=True, slots=True)
class ResolvedSecrets:
    """Secrets resolved for local pipeline runs."""

    openai_api_key: str
    gh_token: str
    anthropic_api_key: str = ""


def resolve_secrets(
    *,
    staging_env: Path = _DEFAULT_STAGING_ENV,
    anthropic_key_file: Path = _DEFAULT_ANTHROPIC_KEY_FILE,
) -> ResolvedSecrets:
    """Resolve all required secrets, raising ``ConfigError`` on failure."""
    openai_key = _resolve_secret("OPENAI_API_KEY", dotenv_file=staging_env)
    if not openai_key:
        raise ConfigError(
            f"OPENAI_API_KEY not found in env or {staging_env}"
        )

    gh_token = _resolve_secret("GH_TOKEN", command=["gh", "auth", "token"])
    if not gh_token:
        raise ConfigError(
            "GH_TOKEN not found. Set the env var or run: gh auth login"
        )

    anthropic_key = _resolve_secret(
        "ANTHROPIC_API_KEY", dotenv_file=anthropic_key_file
    )

    return ResolvedSecrets(
        openai_api_key=openai_key,
        gh_token=gh_token,
        anthropic_api_key=anthropic_key,
    )


# -- Helpers -----------------------------------------------------------

def _build_log_path(
    repo: str,
    pr_num: str,
    prefix: str = "review",
    logs_dir: Path = _DEFAULT_LOGS_DIR,
) -> Path:
    """Build a timestamped log file path inside ``logs/``."""
    logs_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = repo.replace("/", "-")
    return logs_dir / f"{prefix}-{slug}-{pr_num}-{ts}.log"


def _log_header(title: str, fields: dict[str, str]) -> None:
    """Log a formatted run header with auto-aligned fields."""
    width = max(len(k) for k in fields) + 1  # +1 for the colon
    logger.info("=" * 60)
    logger.info(title)
    logger.info("=" * 60)
    for label, value in fields.items():
        logger.info("%-*s %s", width, f"{label}:", value)
    logger.info("")


# -- Public entry points -----------------------------------------------

def run_local_review(
    repo: str,
    pr_num: str,
    variant: str = "frontend",
    deep_mode: object | None = None,
) -> None:
    """Run a full review pipeline locally with automatic secret resolution.

    Output goes to both stdout and a timestamped log file in ``logs/``.
    The review is NOT posted to GitHub (dry-run mode).
    """
    from ..config.review import DeepMode
    if not isinstance(deep_mode, DeepMode):
        deep_mode = DeepMode.AUTO

    secrets = resolve_secrets()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    env_vars: dict[str, str] = {
        "OPENAI_API_KEY": secrets.openai_api_key,
        "GH_TOKEN": secrets.gh_token,
        "REPO": repo,
        "PR_NUM": pr_num,
        "PROMPT_VARIANT": variant,
        "PROMPT_FILE": f"prompts/{variant}.md",
        "GITHUB_RUN_ID": f"local-{ts}",
    }
    if secrets.anthropic_api_key:
        env_vars["ANTHROPIC_API_KEY"] = secrets.anthropic_api_key

    log_path = _build_log_path(repo, pr_num)
    model = OPENAI_MODEL  # SSOT: xair/infra/constants.py

    header_fields: dict[str, str] = {
        "Date": datetime.now().isoformat(),
        "Repo": repo,
        "PR": f"#{pr_num}",
        "Variant": variant,
        "Model": model,
        "Deep": deep_mode.value,
    }
    header_fields["Python"] = sys.version.split()[0]
    header_fields["Log"] = str(log_path)

    with _env_override(env_vars), _pipeline_context(log_path):
        _log_header("AI Reviewer -- Local Dry Run", header_fields)

        from ..config import ReviewConfig
        from ..infra.container import Container
        from ..pipelines.review_via_executor import run_review_full_via_executor

        container = Container.production()
        cfg = ReviewConfig.from_env(dry_run=True, deep_mode=deep_mode)
        # Routed through the executor bridge (parity with __main__.review and
        # dispatch._run_review_gpt). The pipelines.review monolith remains
        # importable for now but has no production caller — see audit at
        # engineering-notes/audits/xair-multi-perspective-2026-05-11/ Finding 1.
        run_review_full_via_executor(container, cfg)

        logger.info("")
        logger.info("Done. Log: %s", log_path)


def run_local_retro(
    repo: str,
    pr_num: str,
    variant: str = "frontend",
    guidance: str = "",
) -> None:
    """Run a full retrospective pipeline locally.

    Same pattern as ``run_local_review``: auto-resolves secrets,
    tees output to a timestamped log file.
    """
    secrets = resolve_secrets()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    env_vars: dict[str, str] = {
        "OPENAI_API_KEY": secrets.openai_api_key,
        "GH_TOKEN": secrets.gh_token,
        "REPO": repo,
        "PR_NUM": pr_num,
        "PROMPT_VARIANT": variant,
        "GITHUB_RUN_ID": f"local-retro-{ts}",
    }

    log_path = _build_log_path(repo, pr_num, prefix="retro")
    model = OPENAI_MODEL  # SSOT: xair/infra/constants.py

    header_fields: dict[str, str] = {
        "Date": datetime.now().isoformat(),
        "Repo": repo,
        "PR": f"#{pr_num}",
        "Variant": variant,
        "Model": model,
    }
    if guidance:
        header_fields["Guidance"] = guidance
    header_fields["Log"] = str(log_path)

    with _env_override(env_vars), _pipeline_context(log_path):
        _log_header("AI Retro -- Local Run", header_fields)

        from ..config.retro import RetroConfig
        from ..infra.container import Container
        from ..pipelines.retro import run_retro

        container = Container.production()
        cfg = RetroConfig.from_env(guidance=guidance)
        run_retro(container, cfg)

        logger.info("")
        logger.info("Done. Log: %s", log_path)

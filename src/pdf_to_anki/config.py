from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MARKER_RGB = (80, 148, 110)
DEFAULT_TOLERANCE = 30


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    anthropic_api_key: str
    claude_model: str = DEFAULT_MODEL
    question_marker_rgb: tuple[int, int, int] = DEFAULT_MARKER_RGB
    question_marker_tolerance: int = DEFAULT_TOLERANCE

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in, "
                "or export ANTHROPIC_API_KEY in your shell."
            )
        return cls(
            anthropic_api_key=key,
            claude_model=os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL),
            question_marker_rgb=_parse_rgb(os.environ.get("QUESTION_MARKER_RGB")),
            question_marker_tolerance=int(
                os.environ.get("QUESTION_MARKER_TOLERANCE", DEFAULT_TOLERANCE)
            ),
        )


def _parse_rgb(raw: str | None) -> tuple[int, int, int]:
    if not raw:
        return DEFAULT_MARKER_RGB
    parts = [int(p.strip()) for p in raw.split(",")]
    if len(parts) != 3:
        raise ConfigError(f"QUESTION_MARKER_RGB must be 'R,G,B', got: {raw!r}")
    return (parts[0], parts[1], parts[2])


def _require(name: str, hint: str = "") -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set.{' ' + hint if hint else ''}")
    return value


@dataclass(frozen=True)
class S3Settings:
    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str = "us-east-1"
    secure: bool = False

    @classmethod
    def load(cls) -> "S3Settings":
        load_dotenv()
        return cls(
            endpoint_url=_require("S3_ENDPOINT_URL", "e.g. http://minio:9000"),
            access_key=_require("S3_ACCESS_KEY"),
            secret_key=_require("S3_SECRET_KEY"),
            bucket=os.environ.get("S3_BUCKET", "pdf-to-anki"),
            region=os.environ.get("S3_REGION", "us-east-1"),
            secure=os.environ.get("S3_SECURE", "false").lower() in {"1", "true", "yes"},
        )


@dataclass(frozen=True)
class BotSettings:
    telegram_bot_token: str
    allowed_usernames: frozenset[str]
    max_upload_bytes: int = 20 * 1024 * 1024

    @classmethod
    def load(cls) -> "BotSettings":
        load_dotenv()
        allowed = parse_usernames(os.environ.get("TELEGRAM_ALLOWED_USERNAMES", ""))
        if not allowed:
            raise ConfigError(
                "TELEGRAM_ALLOWED_USERNAMES is empty. Set it to a comma-separated list "
                "of Telegram handles, e.g. 'alice,bob'. Refusing to start a bot that "
                "would accept nobody."
            )
        return cls(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            allowed_usernames=allowed,
            max_upload_bytes=int(os.environ.get("MAX_UPLOAD_BYTES", 20 * 1024 * 1024)),
        )


def parse_usernames(raw: str) -> frozenset[str]:
    """Normalise a comma/space separated handle list: strips '@', lowercases."""
    parts = re.split(r"[,\s]+", raw or "")
    return frozenset(p.lstrip("@").lower() for p in parts if p.strip())

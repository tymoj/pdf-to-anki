from __future__ import annotations

import os
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

from __future__ import annotations

from pydantic import BaseModel, field_validator


class CleanedCardOut(BaseModel):
    order_index: int
    question_text: str
    answer_html: str

    @field_validator("question_text", "answer_html")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace-only")
        return value

    @field_validator("order_index")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("order_index must be >= 0")
        return value


class CleanupResponse(BaseModel):
    cards: list[CleanedCardOut]

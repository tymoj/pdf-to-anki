from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExtractedImage:
    xref: int
    page_number: int
    bbox: tuple[float, float, float, float]
    image_bytes: bytes
    ext: str

    @property
    def filename(self) -> str:
        return f"img_p{self.page_number}_x{self.xref}.{self.ext}"


@dataclass
class ExtractedCard:
    order_index: int
    question_text: str
    answer_html: str
    source_page: int
    images: list[ExtractedImage] = field(default_factory=list)


@dataclass
class CleanedCard:
    question_text: str
    answer_html: str
    source_page: int
    image_refs: list[str] = field(default_factory=list)

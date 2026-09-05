from __future__ import annotations

import logging
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from html import escape
from typing import Any, Sequence

from pdf_to_anki.models import ExtractedCard, ExtractedImage
from pdf_to_anki.parsing.color import (
    DEFAULT_MARKER_RGB,
    DEFAULT_TOLERANCE,
    RGB,
    is_question_color,
    rgb_from_span_color,
)
from pdf_to_anki.parsing.html import (
    Block,
    Element,
    ImageElement,
    StyledRun,
    normalize_runs,
    render_elements,
)
from pdf_to_anki.parsing.images import assign_images, extract_page_images

logger = logging.getLogger(__name__)

BOLD_WEIGHT = 600
FOOTER_MIN_SIZE = 8.0
FOOTER_MARGIN_PT = 45.0
WORD_GAP_PT = 1.0
LINE_WRAP_RATIO = 1.7
PAGE_WRAP_SLACK_PT = 30.0
INDENT_CLUSTER_PT = 8.0
MAX_LIST_LEVEL = 2
SIZE_TOLERANCE_PT = 0.5
BULLET_SIDE_PT = (3.0, 5.0)
BULLET_SQUARE_PT = 1.0
BULLET_GAP_PT = 2.0

_TERMINAL = re.compile(r"[.!?:]$")

_FONT_XREF = re.compile(r"\((\d+) 0 R\)")
_DESCRIPTOR = re.compile(r"/FontDescriptor (\d+) 0 R")
_WEIGHT = re.compile(r"/FontWeight (\d+)")
_ITALIC_ANGLE = re.compile(r"/ItalicAngle (-?[\d.]+)")


class _FontStyles:
    """span['flags'] is 0 for every span in these Type3 exports, so bold/italic
    must come from the font's FontDescriptor instead."""

    def __init__(self, doc: Any) -> None:
        self._doc = doc
        self._cache: dict[str, tuple[bool, bool]] = {}

    def __call__(self, font_name: str) -> tuple[bool, bool]:
        style = self._cache.get(font_name)
        if style is None:
            style = self._resolve(font_name)
            self._cache[font_name] = style
        return style

    def _resolve(self, font_name: str) -> tuple[bool, bool]:
        match = _FONT_XREF.search(font_name)
        if match:
            try:
                obj = self._doc.xref_object(int(match.group(1)), compressed=True)
                descriptor = _DESCRIPTOR.search(obj)
                if descriptor:
                    fd = self._doc.xref_object(int(descriptor.group(1)), compressed=True)
                    weight = _WEIGHT.search(fd)
                    angle = _ITALIC_ANGLE.search(fd)
                    return (
                        bool(weight) and int(weight.group(1)) >= BOLD_WEIGHT,
                        bool(angle) and float(angle.group(1)) != 0.0,
                    )
            except Exception:
                logger.debug("font descriptor lookup failed for %r", font_name)
        lowered = font_name.lower()
        return ("bold" in lowered, "italic" in lowered or "oblique" in lowered)


@dataclass
class _Line:
    page_number: int
    x0: float
    x1: float
    y0: float
    y1: float
    size: float
    is_question: bool
    runs: list[StyledRun]
    page_max_x1: float = 0.0
    level: int = 0
    has_bullet: bool = False

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


def _line_runs(spans: list[dict], styles: _FontStyles) -> list[StyledRun]:
    runs: list[StyledRun] = []
    prev_x1: float | None = None
    for span in spans:
        if prev_x1 is not None and span["bbox"][0] - prev_x1 > WORD_GAP_PT:
            runs.append(StyledRun(" "))
        bold, italic = styles(span["font"])
        runs.append(StyledRun(span["text"], bold, italic))
        prev_x1 = span["bbox"][2]
    return runs


def _bullet_rects(page: Any) -> list[Any]:
    """List bullets in these exports are vector discs and rings, not text, so the
    only way to tell a real list item from a wrapped line is to find the glyph."""
    low, high = BULLET_SIDE_PT
    rects = []
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        if (
            low <= rect.width <= high
            and low <= rect.height <= high
            and abs(rect.width - rect.height) <= BULLET_SQUARE_PT
        ):
            rects.append(rect)
    return rects


def _mark_bullets(page: Any, lines: list[_Line]) -> None:
    for rect in _bullet_rects(page):
        middle = (rect.y0 + rect.y1) / 2
        for line in lines:
            if line.y0 <= middle <= line.y1 and rect.x1 <= line.x0 + BULLET_GAP_PT:
                line.has_bullet = True
                break


def _page_lines(
    page: Any, styles: _FontStyles, marker_rgb: RGB, tolerance: int
) -> list[_Line]:
    footer_y = page.rect.height - FOOTER_MARGIN_PT
    blocks = [b for b in page.get_text("dict")["blocks"] if b["type"] == 0]
    blocks.sort(key=lambda b: (round(b["bbox"][1]), b["bbox"][0]))

    lines: list[_Line] = []
    for block in blocks:
        for raw in sorted(block["lines"], key=lambda ln: ln["bbox"][1]):
            spans = [s for s in raw["spans"] if s["text"].strip()]
            if not spans:
                continue
            if spans[0]["size"] < FOOTER_MIN_SIZE or raw["bbox"][1] > footer_y:
                continue
            is_question = any(
                is_question_color(rgb_from_span_color(s["color"]), marker_rgb, tolerance)
                for s in spans
            )
            lines.append(
                _Line(
                    page_number=page.number + 1,
                    x0=min(s["bbox"][0] for s in spans),
                    x1=max(s["bbox"][2] for s in spans),
                    y0=raw["bbox"][1],
                    y1=raw["bbox"][3],
                    size=spans[0]["size"],
                    is_question=is_question,
                    runs=_line_runs(spans, styles),
                )
            )

    lines.sort(key=lambda ln: (round(ln.y0), ln.x0))
    _mark_bullets(page, lines)
    page_max_x1 = max((ln.x1 for ln in lines), default=0.0)
    for line in lines:
        line.page_max_x1 = page_max_x1
    return lines


def _indent_reps(x0s: list[float]) -> list[float]:
    reps: list[float] = []
    for x in sorted(x0s):
        if not reps or x - reps[-1] > INDENT_CLUSTER_PT:
            reps.append(x)
    return reps


def _indent_level(x0: float, reps: list[float]) -> int:
    if not reps:
        return 0
    return min(max(bisect_right(reps, x0 + INDENT_CLUSTER_PT) - 1, 0), MAX_LIST_LEVEL)


def _is_continuation(prev: _Line, line: _Line) -> bool:
    if abs(prev.size - line.size) > SIZE_TOLERANCE_PT:
        # A 15pt heading and the 12pt body under it are never one paragraph; the
        # gap test alone decides that pair by less than 1e-05 of a point.
        return False
    if prev.level != line.level:
        # Deeper text carrying no bullet glyph is a wrapped line, not a new item.
        if not (line.level > prev.level and not line.has_bullet):
            return False
    if prev.page_number != line.page_number:
        # No usable vertical gap across a page break: a line that ran to the
        # right margin was wrapped, anything shorter ended its element.
        if prev.page_max_x1 - prev.x1 <= PAGE_WRAP_SLACK_PT:
            return True
        # Prose stopping a few points short of the margin still runs on if the
        # sentence is unfinished. Restricted to level-0 pairs at a page break:
        # list items are lowercase and unpunctuated, so applying it anywhere
        # else chains consecutive bullets into one blob.
        return (
            prev.level == 0
            and line.level == 0
            and not _TERMINAL.search(prev.text.rstrip())
            and line.text.lstrip()[:1].islower()
        )
    return line.y0 - prev.y0 < LINE_WRAP_RATIO * prev.size


def _interleave(
    lines: list[_Line], images: Sequence[ExtractedImage]
) -> list[_Line | ExtractedImage]:
    """Weave images into the reading flow by (page, top edge), keeping `lines` in
    the order the page walk produced them."""
    pending = sorted(images, key=lambda im: (im.page_number, im.bbox[1]))
    merged: list[_Line | ExtractedImage] = []
    index = 0
    for line in lines:
        while index < len(pending) and (
            pending[index].page_number,
            pending[index].bbox[1],
        ) <= (line.page_number, line.y0):
            merged.append(pending[index])
            index += 1
        merged.append(line)
    merged.extend(pending[index:])
    return merged


def _to_elements(items: Sequence[_Line | ExtractedImage]) -> list[Block]:
    elements: list[Block] = []
    current: Element | None = None
    prev: _Line | None = None
    for item in items:
        if isinstance(item, ExtractedImage):
            elements.append(ImageElement(item.filename))
            # An image ends the element it interrupts: the next line starts a new one.
            current, prev = None, None
            continue
        if current is not None and prev is not None and _is_continuation(prev, item):
            if not current.text.endswith("-"):
                current.runs.append(StyledRun(" "))
            current.runs.extend(item.runs)
        else:
            current = Element(level=item.level, runs=list(item.runs))
            elements.append(current)
        prev = item
    return elements


def _joined_text(lines: list[_Line]) -> str:
    runs: list[StyledRun] = []
    for line in lines:
        if runs and not "".join(r.text for r in runs).endswith("-"):
            runs.append(StyledRun(" "))
        runs.extend(line.runs)
    return "".join(r.text for r in normalize_runs(runs))


def extract_cards(
    doc: Any,
    marker_rgb: RGB = DEFAULT_MARKER_RGB,
    tolerance: int = DEFAULT_TOLERANCE,
) -> list[ExtractedCard]:
    styles = _FontStyles(doc)
    lines: list[_Line] = []
    images_by_page: dict[int, list[ExtractedImage]] = {}
    for page in doc:
        lines.extend(_page_lines(page, styles, marker_rgb, tolerance))
        page_images = extract_page_images(doc, page)
        if page_images:
            images_by_page[page.number + 1] = page_images

    reps = _indent_reps([ln.x0 for ln in lines if not ln.is_question])
    for line in lines:
        line.level = 0 if line.is_question else _indent_level(line.x0, reps)

    index = 0
    while index < len(lines) and not lines[index].is_question:
        index += 1
    if index:
        logger.debug("dropping %d line(s) before the first question", index)

    cards: list[ExtractedCard] = []
    answers: list[list[_Line]] = []
    extents: list[dict[int, tuple[float, float]]] = []
    while index < len(lines):
        start = index
        while index < len(lines) and lines[index].is_question:
            index += 1
        question_lines = lines[start:index]

        start = index
        while index < len(lines) and not lines[index].is_question:
            index += 1
        answer_lines = lines[start:index]

        extent: dict[int, tuple[float, float]] = {}
        for line in question_lines + answer_lines:
            low, high = extent.get(line.page_number, (line.y0, line.y1))
            extent[line.page_number] = (min(low, line.y0), max(high, line.y1))

        cards.append(
            ExtractedCard(
                order_index=len(cards),
                # Anki renders the question field as HTML but it carries no
                # markup of its own, so a literal "<CD8>" is content that Anki
                # silently eats unless every bracket is escaped here.
                question_text=escape(_joined_text(question_lines)),
                answer_html="",
                source_page=question_lines[0].page_number,
            )
        )
        answers.append(answer_lines)
        extents.append(extent)

    # Answers render only once their images are known, so each one can be placed
    # where it belongs in the reading flow rather than tacked on at the end.
    assign_images(cards, extents, images_by_page)
    for card, answer_lines in zip(cards, answers):
        card.images.sort(key=lambda im: (im.page_number, im.bbox[1]))
        card.answer_html = render_elements(
            _to_elements(_interleave(answer_lines, card.images))
        )
    return cards

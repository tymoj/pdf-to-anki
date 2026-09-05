from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from typing import Iterable, Sequence, Union

_WS = re.compile(r"\s+")


@dataclass
class StyledRun:
    text: str
    bold: bool = False
    italic: bool = False

    @property
    def style(self) -> tuple[bool, bool]:
        return (self.bold, self.italic)


@dataclass
class Element:
    """One rendered block: a paragraph (level 0) or a list item at depth `level`."""

    level: int = 0
    runs: list[StyledRun] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class ImageElement:
    """One rendered block holding a picture, placed by its position on the page."""

    src: str


Block = Union[Element, ImageElement]


def normalize_runs(runs: Iterable[StyledRun]) -> list[StyledRun]:
    runs = list(runs)
    # A separator space between two identically styled runs belongs inside that
    # styling, so the pair collapses to one tag instead of two.
    for prev, run, nxt in zip(runs, runs[1:], runs[2:]):
        if not run.text.strip() and prev.style == nxt.style:
            run.bold, run.italic = prev.bold, prev.italic

    merged: list[StyledRun] = []
    for run in runs:
        text = _WS.sub(" ", run.text)
        if not text:
            continue
        if merged and merged[-1].style == run.style:
            merged[-1].text += text
        else:
            merged.append(StyledRun(text, run.bold, run.italic))

    for prev, nxt in zip(merged, merged[1:]):
        if prev.text.endswith(" ") and nxt.text.startswith(" "):
            nxt.text = nxt.text.lstrip(" ")
    if merged:
        merged[0].text = merged[0].text.lstrip()
        merged[-1].text = merged[-1].text.rstrip()
    return [r for r in merged if r.text]


def render_runs(runs: Iterable[StyledRun]) -> str:
    parts: list[str] = []
    for run in normalize_runs(runs):
        text = escape(run.text)
        if not run.text.strip():
            parts.append(text)
            continue
        if run.italic:
            text = f"<i>{text}</i>"
        if run.bold:
            text = f"<b>{text}</b>"
        parts.append(text)
    return "".join(parts)


def render_elements(elements: Sequence[Block]) -> str:
    out: list[str] = []
    # The source level each open <ul> stands for, so a list that starts at level
    # 2 with no level-1 parent still keeps its later level-2 items as siblings.
    open_levels: list[int] = []

    def close_all() -> None:
        while open_levels:
            out.append("</li></ul>")
            open_levels.pop()

    for element in elements:
        if isinstance(element, ImageElement):
            close_all()
            out.append(f'<img src="{escape(element.src, quote=True)}">')
            continue

        inner = render_runs(element.runs)
        if not inner.strip():
            continue
        if element.level <= 0:
            close_all()
            out.append(f"<p>{inner}</p>")
            continue

        while open_levels and open_levels[-1] > element.level:
            out.append("</li></ul>")
            open_levels.pop()
        if not open_levels or open_levels[-1] < element.level:
            out.append("<ul>")
            open_levels.append(element.level)
            out.append(f"<li>{inner}")
        else:
            out.append(f"</li><li>{inner}")
    close_all()
    return "".join(out)

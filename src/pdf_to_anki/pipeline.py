from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

import pymupdf

from .anki import write_apkg
from .cleanup import cleanup_cards
from .config import Settings
from .html_images import dedupe_images, img_tag, referenced_images, strip_images
from .models import CleanedCard, ExtractedCard
from .parsing import extract_cards

logger = logging.getLogger(__name__)

GENERIC_TITLES = {"", "untitled", "document"}


def pdf_to_anki(
    pdf_source: str | Path | bytes,
    output_path: str | Path | None = None,
    settings: Settings | None = None,
) -> Path:
    settings = settings or Settings.load()

    if isinstance(pdf_source, bytes):
        doc = pymupdf.open(stream=pdf_source, filetype="pdf")
    else:
        doc = pymupdf.open(pdf_source)

    try:
        extracted = extract_cards(
            doc,
            marker_rgb=settings.question_marker_rgb,
            tolerance=settings.question_marker_tolerance,
        )
        if not extracted:
            raise ValueError(
                "No question-marked text found. Check QUESTION_MARKER_RGB matches the "
                "colour used for questions in this PDF."
            )
        deck_name = _derive_deck_name(doc, pdf_source)
    finally:
        doc.close()

    cleaned = cleanup_cards(extracted, settings)

    out = Path(output_path) if output_path else Path.cwd() / f"{_slug(deck_name)}.apkg"

    with tempfile.TemporaryDirectory() as tmp:
        media_dir = Path(tmp)
        _materialize_images(extracted, cleaned, media_dir)
        return write_apkg(cleaned, deck_name, media_dir, out)


def _materialize_images(
    extracted: list[ExtractedCard],
    cleaned: list[CleanedCard],
    media_dir: Path,
) -> None:
    by_name = {
        img.filename: img for card in extracted for img in card.images
    }
    for card in cleaned:
        # Last gate before the deck, so it also covers the passthrough path that
        # never went through cleanup's reconciliation.
        deduped = dedupe_images(card.answer_html, set())
        if deduped != card.answer_html:
            logger.warning("page %s: removed duplicate <img> tag(s)", card.source_page)
            card.answer_html = deduped

        inline = referenced_images(card.answer_html)

        # A broken-image icon in Anki is worse than no image at all.
        dangling = [ref for ref in inline if ref not in by_name]
        if dangling:
            logger.warning(
                "page %s: dropping <img> tags with no image data: %s",
                card.source_page,
                ", ".join(dangling),
            )
            card.answer_html = strip_images(card.answer_html, dangling)
            inline = [ref for ref in inline if ref not in dangling]

        present: list[str] = []
        for ref in dict.fromkeys([*inline, *card.image_refs]):
            img = by_name.get(ref)
            if img is None:
                logger.warning("card references unknown image %s", ref)
                continue
            (media_dir / ref).write_bytes(img.image_bytes)
            present.append(ref)

        # Cleanup may have lost a tag; an image must never become invisible.
        orphans = [ref for ref in present if ref not in inline]
        if orphans:
            logger.warning(
                "page %s: image(s) missing from answer HTML, appending at the end: %s",
                card.source_page,
                ", ".join(orphans),
            )
            card.answer_html += "".join(img_tag(ref) for ref in orphans)

        card.image_refs = present


def _derive_deck_name(doc: pymupdf.Document, pdf_source: str | Path | bytes) -> str:
    title = (doc.metadata or {}).get("title") or ""
    title = title.strip()
    if title and title.lower() not in GENERIC_TITLES:
        return title
    if isinstance(pdf_source, bytes):
        return "PDF Deck"
    return Path(pdf_source).stem


def _slug(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "deck"

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from pdf_to_anki.models import ExtractedCard, ExtractedImage

logger = logging.getLogger(__name__)

MIN_IMAGE_SIDE_PX = 32

PageExtents = Mapping[int, tuple[float, float]]


def extract_page_images(doc: Any, page: Any) -> list[ExtractedImage]:
    images: list[ExtractedImage] = []
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            bbox = page.get_image_bbox(img)
        except Exception:
            logger.debug("no bbox for image xref=%s on page %s", xref, page.number + 1)
            continue
        try:
            info = doc.extract_image(xref)
        except Exception:
            logger.debug("cannot extract image xref=%s", xref)
            continue
        if not info or not info.get("image"):
            continue
        if min(info.get("width", 0), info.get("height", 0)) < MIN_IMAGE_SIDE_PX:
            continue
        images.append(
            ExtractedImage(
                xref=xref,
                page_number=page.number + 1,
                bbox=(bbox.x0, bbox.y0, bbox.x1, bbox.y1),
                image_bytes=info["image"],
                ext=info.get("ext", "png"),
            )
        )
    return images


def assign_images(
    cards: Sequence[ExtractedCard],
    extents: Sequence[PageExtents],
    images_by_page: Mapping[int, Sequence[ExtractedImage]],
) -> None:
    for page_number, images in images_by_page.items():
        for image in images:
            _, top, _, bottom = image.bbox
            best: ExtractedCard | None = None
            best_overlap = 0.0
            for card, extent in zip(cards, extents):
                span = extent.get(page_number)
                if span is None:
                    continue
                overlap = min(bottom, span[1]) - max(top, span[0])
                if overlap > best_overlap:
                    best, best_overlap = card, overlap
            if best is None:
                # An image can sit above the first text of a continuation page, so it
                # overlaps nothing; fall back to the nearest answer rather than drop it.
                best = _nearest_card(cards, extents, page_number, top, bottom)
            if best is None:
                logger.warning(
                    "dropped image xref=%s on page %s: no card to attach it to",
                    image.xref,
                    page_number,
                )
                continue
            best.images.append(image)


def _nearest_card(
    cards: Sequence[ExtractedCard],
    extents: Sequence[PageExtents],
    page_number: int,
    top: float,
    bottom: float,
) -> ExtractedCard | None:
    best: ExtractedCard | None = None
    best_distance: float | None = None
    for card, extent in zip(cards, extents):
        span = extent.get(page_number)
        if span is None:
            continue
        distance = max(span[0] - bottom, top - span[1], 0.0)
        if best_distance is None or distance < best_distance:
            best, best_distance = card, distance
    if best is not None:
        return best
    # The page carries no answer text at all: attach to the answer still in progress.
    for card, extent in zip(cards, extents):
        if any(page <= page_number for page in extent):
            best = card
    return best

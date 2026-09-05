from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import genanki

from pdf_to_anki.models import CleanedCard

from .notetype import MODEL

logger = logging.getLogger(__name__)


def deck_id_for(deck_name: str) -> int:
    # Deterministic so re-running on the same PDF updates the deck instead of piling up copies.
    digest = hashlib.sha256(deck_name.encode("utf-8")).digest()
    return (int.from_bytes(digest[:8], "big") % (1 << 30)) + (1 << 30)


def write_apkg(
    cards: list[CleanedCard],
    deck_name: str,
    media_dir: Path | None,
    out_path: Path,
) -> Path:
    deck = genanki.Deck(deck_id_for(deck_name), deck_name)

    media_files: list[str] = []
    seen: set[Path] = set()

    for position, card in enumerate(cards):
        deck.add_note(
            genanki.Note(
                model=MODEL,
                fields=[card.question_text, card.answer_html],
                # Position is part of the identity: a deck can legitimately repeat a
                # question on one page, and Anki merges notes that share a GUID.
                guid=genanki.guid_for(card.question_text, card.source_page, position),
            )
        )

        for ref in card.image_refs:
            if media_dir is None:
                logger.warning(
                    "card on page %s references media %r but no media_dir was given",
                    card.source_page,
                    ref,
                )
                continue
            path = (media_dir / ref).resolve()
            if path in seen:
                continue
            seen.add(path)
            if not path.is_file():
                logger.warning("skipping missing media file: %s", path)
                continue
            media_files.append(str(path))

    package = genanki.Package(deck, media_files=media_files)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    package.write_to_file(str(out_path))
    logger.info(
        "wrote %s notes and %s media files to %s", len(deck.notes), len(media_files), out_path
    )
    return out_path

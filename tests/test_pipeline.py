from __future__ import annotations

import json
import re
import sqlite3
import zipfile
from pathlib import Path

import pytest

import pdf_to_anki.pipeline as pipeline
from pdf_to_anki.cleanup.client import _passthrough
from pdf_to_anki.config import Settings
from pdf_to_anki.models import CleanedCard, ExtractedCard, ExtractedImage

PNG = b"\x89PNG\r\n\x1a\nFAKEPIXELS"
IMAGE_PDF = Path(__file__).resolve().parents[1] / "pdf" / "5.praktikum.pdf"


@pytest.fixture
def offline_cleanup(monkeypatch):
    # Same result the pipeline gets when the API is unreachable, so this stays network-free.
    monkeypatch.setattr(pipeline, "cleanup_cards", lambda cards, settings: _passthrough(cards))


def _open_collection(apkg, tmp_path):
    with zipfile.ZipFile(apkg) as zf:
        zf.extract("collection.anki2", tmp_path)
        media = json.loads(zf.read("media"))
    return sqlite3.connect(tmp_path / "collection.anki2"), media


def test_pipeline_writes_deck(sample_pdf, tmp_path, offline_cleanup):
    out = pipeline.pdf_to_anki(
        sample_pdf,
        tmp_path / "deck.apkg",
        settings=Settings(anthropic_api_key="unused"),
    )
    assert out.exists() and out.stat().st_size > 10_000

    con, media = _open_collection(out, tmp_path)
    assert con.execute("select count(*) from notes").fetchone()[0] == 10
    assert media == {}

    decks = json.loads(con.execute("select decks from col").fetchone()[0])
    assert "1. praktikum. Hematopoees" in {d["name"] for d in decks.values()}

    fields = [row[0] for row in con.execute("select flds from notes")]
    joined = "\n".join(fields)
    assert "Palun kirjeldage vererakkude teket." in joined
    assert "<b>hematopoees</b>" in joined
    assert "lümfotsüüt" in joined


def test_pipeline_accepts_bytes(sample_pdf, tmp_path, offline_cleanup):
    out = pipeline.pdf_to_anki(
        sample_pdf.read_bytes(),
        tmp_path / "frombytes.apkg",
        settings=Settings(anthropic_api_key="unused"),
    )
    con, _ = _open_collection(out, tmp_path)
    assert con.execute("select count(*) from notes").fetchone()[0] == 10


def test_pipeline_rejects_pdf_without_markers(tmp_path, offline_cleanup):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "no green text anywhere")
    plain = tmp_path / "plain.pdf"
    doc.save(plain)
    doc.close()

    with pytest.raises(ValueError, match="QUESTION_MARKER_RGB"):
        pipeline.pdf_to_anki(
            plain, tmp_path / "x.apkg", settings=Settings(anthropic_api_key="unused")
        )


def _image(xref: int) -> ExtractedImage:
    return ExtractedImage(
        xref=xref, page_number=1, bbox=(0.0, 0.0, 10.0, 10.0), image_bytes=PNG, ext="png"
    )


def _extracted(*images: ExtractedImage) -> list[ExtractedCard]:
    return [
        ExtractedCard(
            order_index=0,
            question_text="K?",
            answer_html="<p>V</p>",
            source_page=1,
            images=list(images),
        )
    ]


def test_materialize_keeps_an_inline_tag_in_place_without_duplicating_it(tmp_path):
    html = '<p>Enne</p><img src="img_p1_x11.png"><p>Pärast</p>'
    cleaned = [CleanedCard("K?", html, 1, ["img_p1_x11.png"])]

    pipeline._materialize_images(_extracted(_image(11)), cleaned, tmp_path)

    assert cleaned[0].answer_html == html
    assert cleaned[0].image_refs == ["img_p1_x11.png"]
    assert (tmp_path / "img_p1_x11.png").read_bytes() == PNG


def test_materialize_is_idempotent(tmp_path):
    html = '<p>Enne</p><img src="img_p1_x11.png"><p>Pärast</p>'
    cleaned = [CleanedCard("K?", html, 1, ["img_p1_x11.png"])]
    extracted = _extracted(_image(11))

    pipeline._materialize_images(extracted, cleaned, tmp_path)
    pipeline._materialize_images(extracted, cleaned, tmp_path)

    assert cleaned[0].answer_html.count("<img") == 1
    assert cleaned[0].image_refs == ["img_p1_x11.png"]


def test_materialize_appends_an_image_missing_from_the_html(tmp_path, caplog):
    cleaned = [CleanedCard("K?", "<p>V</p>", 1, ["img_p1_x11.png"])]

    with caplog.at_level("WARNING"):
        pipeline._materialize_images(_extracted(_image(11)), cleaned, tmp_path)

    assert cleaned[0].answer_html == '<p>V</p><img src="img_p1_x11.png">'
    assert cleaned[0].image_refs == ["img_p1_x11.png"]
    assert "missing from answer HTML" in caplog.text

    # Re-running must not append it a second time.
    pipeline._materialize_images(_extracted(_image(11)), cleaned, tmp_path)
    assert cleaned[0].answer_html.count("<img") == 1


def test_materialize_strips_a_dangling_tag_with_no_bytes(tmp_path, caplog):
    html = '<p>Enne</p><img src="gone.png"><p>Pärast</p>'
    cleaned = [CleanedCard("K?", html, 1, [])]

    with caplog.at_level("WARNING"):
        pipeline._materialize_images(_extracted(), cleaned, tmp_path)

    assert cleaned[0].answer_html == "<p>Enne</p><p>Pärast</p>"
    assert cleaned[0].image_refs == []
    assert "no image data" in caplog.text


def test_materialize_adopts_an_inline_image_absent_from_image_refs(tmp_path):
    html = '<p>Enne</p><img src="img_p1_x11.png"><p>Pärast</p>'
    cleaned = [CleanedCard("K?", html, 1, [])]

    pipeline._materialize_images(_extracted(_image(11)), cleaned, tmp_path)

    assert cleaned[0].answer_html == html
    assert cleaned[0].image_refs == ["img_p1_x11.png"]


@pytest.mark.skipif(not IMAGE_PDF.exists(), reason=f"sample PDF missing: {IMAGE_PDF}")
def test_pipeline_keeps_images_inline_end_to_end(tmp_path, offline_cleanup):
    out = pipeline.pdf_to_anki(
        IMAGE_PDF,
        tmp_path / "images.apkg",
        settings=Settings(anthropic_api_key="unused"),
    )

    con, media = _open_collection(out, tmp_path)
    assert len(media) == 4
    assert all(name.startswith("img_p") and name.endswith(".png") for name in media.values())

    answers = [row[0].split("\x1f")[1] for row in con.execute("select flds from notes")]
    with_images = [a for a in answers if "<img" in a]
    assert len(with_images) == 4
    for answer in with_images:
        last = list(re.finditer(r"<img[^>]*>", answer))[-1]
        # Inline, not dumped at the end: prose still follows the final image.
        assert answer[last.end() :].strip()


def test_materialize_collapses_a_duplicated_tag(tmp_path, caplog):
    doubled = '<p>Enne</p><img src="img_p1_x11.png"><p>Pärast</p><img src="img_p1_x11.png">'
    cleaned = [CleanedCard("K?", doubled, 1, ["img_p1_x11.png"])]

    with caplog.at_level("WARNING"):
        pipeline._materialize_images(_extracted(_image(11)), cleaned, tmp_path)

    assert cleaned[0].answer_html == '<p>Enne</p><img src="img_p1_x11.png"><p>Pärast</p>'
    assert cleaned[0].image_refs == ["img_p1_x11.png"]
    assert "duplicate" in caplog.text

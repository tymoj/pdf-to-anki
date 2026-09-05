from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pdf_to_anki.bot import convert
from pdf_to_anki.storage import StorageError


@pytest.fixture
def bot() -> MagicMock:
    b = MagicMock()
    b.send_document = AsyncMock()
    b.send_message = AsyncMock()
    return b


@pytest.fixture
def storage() -> MagicMock:
    s = MagicMock()
    s.put_pdf.return_value = "pdfs/1/j.pdf"
    s.put_deck.return_value = "decks/1/j.apkg"
    return s


def run(bot, storage, **kw):
    params = dict(chat_id=5, user_id=1, job_id="j", filename="prakt.pdf", pdf_bytes=b"%PDF")
    params.update(kw)
    asyncio.run(convert.convert_and_deliver(bot, storage, **params))


def fake_pipeline(deck_bytes: bytes = b"apkg-data"):
    def _run(pdf_bytes, out_path):
        Path(out_path).write_bytes(deck_bytes)
        return Path(out_path)
    return _run


def test_happy_path_stores_and_sends(bot, storage, monkeypatch):
    monkeypatch.setattr(convert, "run_pipeline", fake_pipeline())

    run(bot, storage)

    storage.put_pdf.assert_called_once_with(1, "j", b"%PDF")
    storage.put_deck.assert_called_once()
    assert storage.put_deck.call_args.args[2].name == "prakt.apkg"
    bot.send_document.assert_awaited_once()
    assert bot.send_document.await_args.kwargs["filename"] == "prakt.apkg"


def test_temp_files_are_cleaned_up(bot, storage, monkeypatch):
    captured: dict = {}

    def _run(pdf_bytes, out_path):
        captured["dir"] = Path(out_path).parent
        Path(out_path).write_bytes(b"x")
        return Path(out_path)

    monkeypatch.setattr(convert, "run_pipeline", _run)
    run(bot, storage)

    assert not captured["dir"].exists()


def test_oversize_deck_sends_a_link_instead(bot, storage, monkeypatch):
    monkeypatch.setattr(convert, "run_pipeline", fake_pipeline(b"x"))
    monkeypatch.setattr(convert, "TELEGRAM_MAX_DOCUMENT_BYTES", 0)
    storage.presigned_url.return_value = "https://minio.example/deck?sig=1"

    run(bot, storage)

    bot.send_document.assert_not_awaited()
    assert "https://minio.example/deck?sig=1" in bot.send_message.await_args.kwargs["text"]


def test_pipeline_failure_notifies_the_user_and_never_raises(bot, storage, monkeypatch, caplog):
    def boom(pdf_bytes, out_path):
        raise ValueError("No question-marked text found. Check QUESTION_MARKER_RGB")

    monkeypatch.setattr(convert, "run_pipeline", boom)

    with caplog.at_level(logging.ERROR, logger="pdf_to_anki.bot.convert"):
        run(bot, storage)  # must not raise

    text = bot.send_message.await_args.kwargs["text"]
    assert "prakt.pdf" in text and "QUESTION_MARKER_RGB" in text
    assert "job j failed" in caplog.text


def test_storage_failure_is_explained_in_plain_language(bot, storage, monkeypatch):
    storage.put_pdf.side_effect = StorageError("minio down")
    monkeypatch.setattr(convert, "run_pipeline", fake_pipeline())

    run(bot, storage)

    assert "Storage is unavailable" in bot.send_message.await_args.kwargs["text"]
    bot.send_document.assert_not_awaited()


def test_a_failed_apology_does_not_escape(bot, storage, monkeypatch):
    monkeypatch.setattr(convert, "run_pipeline", MagicMock(side_effect=RuntimeError("x")))
    bot.send_message.side_effect = RuntimeError("telegram down")

    run(bot, storage)  # the point: still no exception


@pytest.mark.parametrize(
    "source,expected",
    [
        ("5. praktikum.pdf", "5._praktikum.apkg"),
        ("a/b.pdf", "b.apkg"),
        (".pdf", "deck.apkg"),
        ("", "deck.apkg"),
    ],
)
def test_deck_filename(source, expected):
    assert convert.deck_filename(source) == expected

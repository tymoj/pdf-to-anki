"""Convert one uploaded PDF and deliver the deck back over Telegram.

Runs as a background task inside the bot process. The conversion itself is
blocking (PyMuPDF parsing plus one Claude call, together a minute or two), so it
is pushed onto a thread; the event loop stays free to answer other users.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import anthropic
import pymupdf
import telegram

from ..config import ConfigError, S3Settings
from ..pipeline import pdf_to_anki as run_pipeline
from ..storage import Storage, StorageError

logger = logging.getLogger(__name__)

# The Bot API refuses documents larger than this. Past it we hand out a
# pre-signed link instead of the file itself.
TELEGRAM_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024

# Long enough that someone can come back to the link after reading the message.
PRESIGNED_URL_TTL_SECONDS = 24 * 3600

# Checked most-specific first: telegram.error.BadRequest subclasses NetworkError,
# and anthropic's 4xx errors subclass the same APIStatusError as its 5xx ones.
_EXPLANATIONS: tuple[tuple[type[BaseException], str], ...] = (
    (pymupdf.FileDataError, "That file could not be read as a PDF - it may be corrupt or password-protected."),
    (ValueError, ""),  # the pipeline's own message names the marker colour to check
    (ConfigError, "The server is misconfigured. The details are in its logs."),
    (telegram.error.Forbidden, "I cannot message you - unblock the bot and try again."),
    (StorageError, "Storage is unavailable at the moment. Send it again in a few minutes."),
    (anthropic.AuthenticationError, "The server's Claude credentials were rejected. The details are in its logs."),
    (anthropic.RateLimitError, "Claude is rate-limiting right now. Try again shortly."),
    (anthropic.APIConnectionError, "Could not reach Claude. Try again shortly."),
)


async def convert_and_deliver(
    bot: telegram.Bot,
    storage: Storage,
    *,
    chat_id: int,
    user_id: int,
    job_id: str,
    filename: str,
    pdf_bytes: bytes,
) -> None:
    """Never raises: a failure is reported to the user and logged, not propagated."""
    logger.info(
        "job %s: converting %r (%d bytes) for user %s", job_id, filename, len(pdf_bytes), user_id
    )
    try:
        deck_key = await _run(
            bot,
            storage,
            chat_id=chat_id,
            user_id=user_id,
            job_id=job_id,
            filename=filename,
            pdf_bytes=pdf_bytes,
        )
        logger.info("job %s: delivered %s to chat %s", job_id, deck_key, chat_id)
    except Exception as exc:
        logger.error("job %s failed: %s", job_id, exc, exc_info=True)
        await _notify_failure(bot, chat_id, filename, exc)


async def _run(
    bot: telegram.Bot,
    storage: Storage,
    *,
    chat_id: int,
    user_id: int,
    job_id: str,
    filename: str,
    pdf_bytes: bytes,
) -> str:
    await asyncio.to_thread(storage.put_pdf, user_id, job_id, pdf_bytes)

    # mkdtemp rather than TemporaryDirectory: the .apkg has to outlive the
    # conversion call so it can be uploaded and then sent.
    workdir = Path(tempfile.mkdtemp(prefix="pdf-to-anki-"))
    try:
        deck_path = workdir / deck_filename(filename)
        # Blocking: PyMuPDF parsing plus one Claude call. Run inline it would
        # stall the event loop, freezing the bot for every other user.
        await asyncio.to_thread(run_pipeline, pdf_bytes, deck_path)
        deck_key = await asyncio.to_thread(storage.put_deck, user_id, job_id, deck_path)
        await _deliver(bot, storage, chat_id, deck_path, deck_key)
        return deck_key
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _deliver(
    bot: telegram.Bot, storage: Storage, chat_id: int, deck_path: Path, deck_key: str
) -> None:
    size = deck_path.stat().st_size
    if size > TELEGRAM_MAX_DOCUMENT_BYTES:
        url = await asyncio.to_thread(_download_url, storage, deck_key)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"{deck_path.name} came out at {size / 1024 / 1024:.0f} MB, over the "
                f"{TELEGRAM_MAX_DOCUMENT_BYTES // 1024 // 1024} MB limit for files a "
                f"bot can send. Download it here instead - the link is good for "
                f"{PRESIGNED_URL_TTL_SECONDS // 3600} hours:\n{url}"
            ),
        )
        return

    with deck_path.open("rb") as handle:
        await bot.send_document(
            chat_id=chat_id,
            document=handle,
            filename=deck_path.name,
            caption="Import this into Anki.",
        )


async def _notify_failure(
    bot: telegram.Bot, chat_id: int, filename: str, exc: BaseException
) -> None:
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"Could not turn {filename} into a deck.\n\n{explain(exc)}",
        )
    except Exception:  # a failed apology must not mask the failure it apologises for
        logger.exception("could not deliver the failure notice to chat %s", chat_id)


def explain(exc: BaseException) -> str:
    """Plain language for the user. Never echoes anything but our own messages."""
    for exc_type, message in _EXPLANATIONS:
        if isinstance(exc, exc_type):
            if message:
                return message
            return str(exc) or "No question-marked text was found in that PDF."
    return "Something went wrong on the server. The details are in its logs."


def deck_filename(source_filename: str) -> str:
    """`.apkg` name for the upload. The pipeline is handed bytes, so it cannot
    recover a name from a path and would fall back to a generic one."""
    # Leading dots are stripped too: Path(".pdf").stem is ".pdf", and a deck that
    # arrives as a hidden file is one the user cannot find.
    stem = re.sub(r"[^\w.-]+", "_", Path(source_filename).stem).strip("._")
    return f"{stem or 'deck'}.apkg"


def _download_url(storage: Storage, key: str) -> str:
    # presigned_url signs against the configured endpoint, which inside compose is
    # the private hostname http://minio:9000 - unreachable for a Telegram user.
    # Sign against the published address when there is one.
    public = os.environ.get("S3_PUBLIC_ENDPOINT_URL")
    if public and public != storage.settings.endpoint_url:
        storage = Storage(replace(storage.settings, endpoint_url=public))
    return storage.presigned_url(key, expires_in=PRESIGNED_URL_TTL_SECONDS)

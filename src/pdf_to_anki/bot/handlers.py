"""Telegram handlers: /start, /help and the PDF upload entry point."""

from __future__ import annotations

import functools
import logging
import uuid
from typing import Awaitable, Callable

from telegram import Document, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from ..config import BotSettings
from .auth import Authorizer
from .convert import convert_and_deliver

logger = logging.getLogger(__name__)

# Keys under which main.py stashes shared objects on Application.bot_data.
SETTINGS_KEY = "settings"
AUTHORIZER_KEY = "authorizer"
STORAGE_KEY = "storage"

#: The Bot API refuses getFile for anything larger, whatever MAX_UPLOAD_BYTES says.
TELEGRAM_GETFILE_LIMIT_BYTES = 20 * 1024 * 1024

_AUTH_MARKER = "__pdf_to_anki_auth_checked__"

WELCOME = (
    "I turn a colour-marked PDF practicum into an Anki deck.\n\n"
    "Send me the PDF as a file (not a photo). Questions are the passages marked "
    "in the highlight colour; everything under them becomes the answer.\n\n"
    "Conversion takes a minute or two; I'll send the .apkg back when it's done.\n\n"
    "Commands: /start, /help"
)

DENIED = "Sorry, you're not on this bot's allowlist."

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def requires_auth(func: Handler) -> Handler:
    """Refuse the update unless the sender passes :class:`Authorizer`.

    Every handler registered through :func:`register_handlers` must carry this;
    the registration helper rejects any that does not, so a future handler cannot
    quietly ship without an access check.
    """

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        authorizer: Authorizer | None = context.bot_data.get(AUTHORIZER_KEY)
        if authorizer is None:
            logger.error("no authorizer configured; refusing update")
            await _reply(update, DENIED)
            return
        if not authorizer.authorize(update.effective_user):
            await _reply(update, DENIED)
            return
        await func(update, context)

    setattr(wrapper, _AUTH_MARKER, True)
    return wrapper


@requires_auth
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, WELCOME)


@requires_auth
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, WELCOME)


@requires_auth
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    document: Document | None = getattr(message, "document", None)
    if message is None or document is None:
        return

    if not _looks_like_pdf(document):
        await _reply(
            update,
            "That doesn't look like a PDF. Send the practicum as a .pdf file.",
        )
        return

    settings: BotSettings = context.bot_data[SETTINGS_KEY]
    limit = min(settings.max_upload_bytes, TELEGRAM_GETFILE_LIMIT_BYTES)
    size = document.file_size
    if size is not None and size > limit:
        await _reply(
            update,
            f"That file is {_human_size(size)}; my limit is {_human_size(limit)}. "
            "Telegram's Bot API won't let me download files larger than 20 MB, "
            "so please split the PDF or compress it.",
        )
        return

    user = update.effective_user
    job_id = uuid.uuid4().hex
    filename = document.file_name or f"{job_id}.pdf"

    try:
        telegram_file = await document.get_file()
        pdf_bytes = bytes(await telegram_file.download_as_bytearray())
    except Exception:
        logger.exception("failed to download %s for user id=%s", filename, user.id if user else None)
        await _reply(update, "I couldn't download that file from Telegram. Please try again.")
        return

    storage = context.bot_data.get(STORAGE_KEY)
    if storage is None:
        logger.error("no storage configured; cannot accept %s", filename)
        await _reply(
            update,
            "The conversion service is temporarily unavailable. Please try again in a few minutes.",
        )
        return

    await _reply(
        update,
        f"Got {filename} — converting now. This takes a minute or two; "
        f"I'll send the deck back when it's done.",
    )

    # Detached deliberately: the conversion outlives this handler so the bot stays
    # responsive to everyone else. convert_and_deliver never raises - it reports
    # its own failures to the user.
    context.application.create_task(
        convert_and_deliver(
            context.bot,
            storage,
            chat_id=message.chat_id,
            user_id=user.id if user else 0,
            job_id=job_id,
            filename=filename,
            pdf_bytes=pdf_bytes,
        )
    )
    logger.info(
        "started job %s (%s, %d bytes) for user id=%s",
        job_id,
        filename,
        len(pdf_bytes),
        user.id if user else None,
    )


@requires_auth
async def handle_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, "Send me a PDF file and I'll turn it into an Anki deck. /help for details.")


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", _checked(start)))
    app.add_handler(CommandHandler("help", _checked(help_command)))
    # Document.ALL rather than Document.PDF so non-PDFs get an explanation instead
    # of silence.
    app.add_handler(MessageHandler(filters.Document.ALL, _checked(handle_document)))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _checked(handle_unsupported))
    )


def _checked(handler: Handler) -> Handler:
    if not getattr(handler, _AUTH_MARKER, False):
        raise RuntimeError(
            f"{getattr(handler, '__name__', handler)!r} is not wrapped in @requires_auth"
        )
    return handler


def _human_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / 1024 / 1024:.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def _looks_like_pdf(document: Document) -> bool:
    mime = (document.mime_type or "").lower()
    name = (document.file_name or "").lower()
    return mime == "application/pdf" or name.endswith(".pdf")


async def _reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(text)

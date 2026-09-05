"""Entry point for the Telegram bot: ``python -m pdf_to_anki.bot.main``."""

from __future__ import annotations

import logging

from telegram.ext import Application, ApplicationBuilder

from ..config import BotSettings, S3Settings
from ..storage import Storage
from .auth import Authorizer
from .pins import load_pins, save_pins
from .handlers import AUTHORIZER_KEY, SETTINGS_KEY, STORAGE_KEY, register_handlers

logger = logging.getLogger(__name__)


def build_application(settings: BotSettings, storage: Storage | None = None) -> Application:
    # concurrent_updates: conversions are detached background tasks, but this also
    # keeps one user's upload from delaying anyone else's commands.
    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .build()
    )
    app.bot_data[SETTINGS_KEY] = settings
    app.bot_data[AUTHORIZER_KEY] = _build_authorizer(settings, storage)
    if storage is not None:
        app.bot_data[STORAGE_KEY] = storage
    register_handlers(app)
    return app


def _build_authorizer(settings: BotSettings, storage: Storage | None) -> Authorizer:
    """Authorizer whose pins survive a restart, when storage is reachable.

    Without persistence every restart re-opens first-contact resolution, which is
    exactly the window a freed-handle takeover needs. Storage being unavailable
    degrades to in-memory pins rather than blocking the bot from starting.
    """
    if storage is None:
        logger.warning("no storage: auth pins will not survive a restart")
        return Authorizer(settings.allowed_usernames)
    pins = load_pins(storage)
    if pins:
        logger.info("restored %d auth pin(s) from storage", len(pins))
    return Authorizer(
        settings.allowed_usernames,
        pins=pins,
        on_pin=lambda snapshot: save_pins(storage, snapshot),
    )


def _build_storage() -> Storage | None:
    """None rather than raising: the bot should still answer /help and explain
    itself when object storage is down, instead of crash-looping."""
    try:
        storage = Storage(S3Settings.load())
        storage.ensure_bucket()
        logger.info("storage ready: bucket %r", storage.bucket)
        return storage
    except Exception as exc:
        logger.error("storage unavailable (%s): %s", type(exc).__name__, exc)
        return None




def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = BotSettings.load()
    logger.info("allowlist holds %d handle(s)", len(settings.allowed_usernames))
    build_application(settings, _build_storage()).run_polling()


if __name__ == "__main__":
    main()

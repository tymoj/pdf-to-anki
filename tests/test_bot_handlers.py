from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from pdf_to_anki.bot import handlers
from pdf_to_anki.bot.auth import Authorizer
from pdf_to_anki.config import BotSettings, parse_usernames

PDF_BYTES = b"%PDF-1.7 fake"
CHAT_ID = 555
ALICE_ID = 1001


@pytest.fixture
def settings() -> BotSettings:
    return BotSettings(
        telegram_bot_token="not-a-real-token",
        allowed_usernames=parse_usernames("alice"),
        max_upload_bytes=1024,
    )


@pytest.fixture
def storage() -> MagicMock:
    return MagicMock()


@pytest.fixture
def context(settings: BotSettings, storage: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.bot_data = {
        handlers.SETTINGS_KEY: settings,
        handlers.AUTHORIZER_KEY: Authorizer(settings.allowed_usernames),
        handlers.STORAGE_KEY: storage,
    }
    # create_task would otherwise return a MagicMock and leave the coroutine
    # un-awaited; close it so tests don't emit "never awaited" warnings.
    ctx.application.create_task = MagicMock(side_effect=lambda coro: coro.close())
    return ctx


def make_document(
    *,
    file_name: str = "practicum.pdf",
    mime_type: str | None = "application/pdf",
    file_size: int | None = len(PDF_BYTES),
    payload: bytes = PDF_BYTES,
) -> MagicMock:
    telegram_file = MagicMock()
    telegram_file.download_as_bytearray = AsyncMock(return_value=bytearray(payload))
    document = MagicMock()
    document.file_name = file_name
    document.mime_type = mime_type
    document.file_size = file_size
    document.get_file = AsyncMock(return_value=telegram_file)
    return document


def make_update(
    *,
    user_id: int = ALICE_ID,
    username: str | None = "alice",
    document: MagicMock | None = None,
) -> MagicMock:
    message = MagicMock()
    message.chat_id = CHAT_ID
    message.document = document
    message.reply_text = AsyncMock()
    update = MagicMock()
    update.effective_message = message
    update.effective_user = SimpleNamespace(id=user_id, username=username)
    return update


def replies(update: MagicMock) -> list[str]:
    return [call.args[0] for call in update.effective_message.reply_text.call_args_list]


def test_start_greets_an_allowed_user(context: MagicMock) -> None:
    update = make_update()

    asyncio.run(handlers.start(update, context))

    assert "Anki deck" in replies(update)[0]


def test_start_refuses_an_unlisted_user(
    context: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    update = make_update(user_id=666, username="mallory")

    with caplog.at_level(logging.WARNING, logger="pdf_to_anki.bot.auth"):
        asyncio.run(handlers.start(update, context))

    assert replies(update) == [handlers.DENIED]
    assert "666" in caplog.text and "mallory" in caplog.text


def test_unauthorized_upload_is_refused_and_nothing_is_enqueued(
    context: MagicMock, storage: MagicMock
) -> None:
    document = make_document()
    update = make_update(user_id=666, username="mallory", document=document)

    asyncio.run(handlers.handle_document(update, context))

    assert replies(update) == [handlers.DENIED]
    context.application.create_task.assert_not_called()
    document.get_file.assert_not_called()


def test_missing_authorizer_refuses_everything(context: MagicMock, storage: MagicMock) -> None:
    context.bot_data[handlers.AUTHORIZER_KEY] = None
    update = make_update(document=make_document())

    asyncio.run(handlers.handle_document(update, context))

    assert replies(update) == [handlers.DENIED]
    context.application.create_task.assert_not_called()


def test_non_pdf_is_refused_without_downloading(context: MagicMock, storage: MagicMock) -> None:
    document = make_document(file_name="notes.docx", mime_type="application/msword")
    update = make_update(document=document)

    asyncio.run(handlers.handle_document(update, context))

    assert "doesn't look like a PDF" in replies(update)[0]
    document.get_file.assert_not_called()
    context.application.create_task.assert_not_called()


def test_pdf_extension_without_mime_type_is_accepted(
    context: MagicMock, storage: MagicMock
) -> None:
    update = make_update(document=make_document(mime_type=None))

    asyncio.run(handlers.handle_document(update, context))

    context.application.create_task.assert_called_once()


def test_oversize_is_refused_before_downloading(
    context: MagicMock, storage: MagicMock, settings: BotSettings
) -> None:
    document = make_document(file_size=settings.max_upload_bytes + 1)
    update = make_update(document=document)

    asyncio.run(handlers.handle_document(update, context))

    reply = replies(update)[0]
    assert "limit" in reply and "20 MB" in reply
    document.get_file.assert_not_called()
    context.application.create_task.assert_not_called()


def test_telegram_download_cap_applies_even_when_configured_higher(
    context: MagicMock, storage: MagicMock
) -> None:
    context.bot_data[handlers.SETTINGS_KEY] = BotSettings(
        telegram_bot_token="not-a-real-token",
        allowed_usernames=parse_usernames("alice"),
        max_upload_bytes=100 * 1024 * 1024,
    )
    document = make_document(file_size=handlers.TELEGRAM_GETFILE_LIMIT_BYTES + 1)
    update = make_update(document=document)

    asyncio.run(handlers.handle_document(update, context))

    document.get_file.assert_not_called()
    context.application.create_task.assert_not_called()


def test_valid_pdf_starts_one_conversion_with_the_upload(
    context: MagicMock, storage: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict = {}

    async def _noop() -> None:
        return None

    def fake_convert(bot, store, **kwargs):
        # Recorded when called, not when awaited: the handler detaches the
        # coroutine and this test never runs it.
        seen.update(kwargs)
        seen["storage"] = store
        return _noop()

    monkeypatch.setattr(handlers, "convert_and_deliver", fake_convert)
    update = make_update(document=make_document())

    asyncio.run(handlers.handle_document(update, context))

    context.application.create_task.assert_called_once()
    assert seen["chat_id"] == CHAT_ID
    assert seen["user_id"] == ALICE_ID
    assert seen["filename"] == "practicum.pdf"
    assert seen["pdf_bytes"] == PDF_BYTES
    assert isinstance(seen["pdf_bytes"], bytes)
    assert seen["storage"] is storage

    job_id = seen["job_id"]
    assert isinstance(job_id, str) and len(job_id) == 32
    int(job_id, 16)  # uuid4().hex

    # The user is told immediately, before the slow work starts.
    assert "practicum.pdf" in replies(update)[0]


def test_each_upload_gets_a_fresh_job_id(
    context: MagicMock, storage: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_ids: set[str] = set()

    async def _noop() -> None:
        return None

    def fake_convert(bot, store, **kwargs):
        job_ids.add(kwargs["job_id"])
        return _noop()

    monkeypatch.setattr(handlers, "convert_and_deliver", fake_convert)
    for _ in range(2):
        asyncio.run(handlers.handle_document(make_update(document=make_document()), context))

    assert len(job_ids) == 2


def test_missing_storage_replies_politely(context: MagicMock) -> None:
    context.bot_data[handlers.STORAGE_KEY] = None
    update = make_update(document=make_document())

    asyncio.run(handlers.handle_document(update, context))

    assert "temporarily unavailable" in replies(update)[0]
    context.application.create_task.assert_not_called()


def test_download_failure_replies_politely(context: MagicMock, storage: MagicMock) -> None:
    document = make_document()
    document.get_file.side_effect = RuntimeError("network gone")
    update = make_update(document=document)

    asyncio.run(handlers.handle_document(update, context))

    assert "couldn't download" in replies(update)[0]
    context.application.create_task.assert_not_called()


def test_renamed_pinned_user_can_still_upload(context: MagicMock, storage: MagicMock) -> None:
    asyncio.run(handlers.start(make_update(), context))

    renamed = make_update(username="alice_moved_on", document=make_document())
    asyncio.run(handlers.handle_document(renamed, context))

    context.application.create_task.assert_called_once()


def test_handle_hijacker_is_refused_after_the_real_user_is_pinned(
    context: MagicMock, storage: MagicMock
) -> None:
    asyncio.run(handlers.start(make_update(), context))

    hijacker = make_update(user_id=666, username="alice", document=make_document())
    asyncio.run(handlers.handle_document(hijacker, context))

    assert replies(hijacker) == [handlers.DENIED]
    context.application.create_task.assert_not_called()


def test_every_registered_handler_is_auth_wrapped() -> None:
    app = MagicMock()
    registered = []
    app.add_handler.side_effect = lambda handler: registered.append(handler)

    handlers.register_handlers(app)

    assert registered
    for handler in registered:
        assert getattr(handler.callback, "__pdf_to_anki_auth_checked__", False)


def test_registering_an_unwrapped_handler_raises() -> None:
    async def naked(update, context) -> None:  # pragma: no cover - never invoked
        pass

    with pytest.raises(RuntimeError, match="requires_auth"):
        handlers._checked(naked)

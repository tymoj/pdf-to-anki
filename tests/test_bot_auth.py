from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from pdf_to_anki.bot.auth import Authorizer
from pdf_to_anki.config import parse_usernames


def user(user_id: int, username: str | None) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, username=username)


@pytest.fixture
def auth() -> Authorizer:
    return Authorizer(parse_usernames("@Alice, bob"))


def test_allowed_handle_first_contact_is_allowed_and_pinned(auth: Authorizer) -> None:
    assert auth.authorize(user(1001, "alice")) is True
    assert auth.snapshot() == {"alice": 1001}


def test_pinned_user_keeps_access_after_renaming(auth: Authorizer) -> None:
    auth.authorize(user(1001, "alice"))

    assert auth.authorize(user(1001, "alice_moved_on")) is True
    # Renaming does not add a second pin, nor rewrite the first.
    assert auth.snapshot() == {"alice": 1001}


def test_different_user_presenting_pinned_handle_is_denied(
    auth: Authorizer, caplog: pytest.LogCaptureFixture
) -> None:
    auth.authorize(user(1001, "alice"))

    with caplog.at_level(logging.WARNING, logger="pdf_to_anki.bot.auth"):
        assert auth.authorize(user(666, "alice")) is False

    message = caplog.text
    assert "1001" in message and "666" in message
    assert auth.snapshot() == {"alice": 1001}


def test_handle_not_on_allowlist_is_denied(
    auth: Authorizer, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="pdf_to_anki.bot.auth"):
        assert auth.authorize(user(2002, "mallory")) is False

    assert "2002" in caplog.text and "mallory" in caplog.text
    assert auth.snapshot() == {}


def test_missing_username_without_pin_is_denied(auth: Authorizer) -> None:
    assert auth.authorize(user(3003, None)) is False


def test_missing_username_with_pin_is_allowed(auth: Authorizer) -> None:
    auth.authorize(user(1001, "alice"))

    assert auth.authorize(user(1001, None)) is True


def test_absent_user_is_denied(auth: Authorizer) -> None:
    assert auth.authorize(None) is False


def test_handles_are_case_insensitive_and_at_sign_is_stripped() -> None:
    auth = Authorizer(parse_usernames("@Alice"))

    assert auth.authorize(user(1001, "@ALICE")) is True
    assert auth.snapshot() == {"alice": 1001}


def test_snapshot_restore_round_trip(auth: Authorizer) -> None:
    auth.authorize(user(1001, "alice"))
    auth.authorize(user(2002, "bob"))
    saved = auth.snapshot()

    revived = Authorizer(parse_usernames("alice bob"))
    revived.restore(saved)

    assert revived.snapshot() == saved


def test_restored_pins_take_effect() -> None:
    revived = Authorizer(parse_usernames("alice"), pins={"alice": 1001})

    # The pinned id is allowed even under a different handle...
    assert revived.authorize(user(1001, "renamed")) is True
    # ...and the handle it was pinned to no longer buys anyone else access.
    assert revived.authorize(user(666, "alice")) is False


def test_restore_drops_handles_no_longer_allowed() -> None:
    revived = Authorizer(parse_usernames("alice"), pins={"bob": 2002, "alice": 1001})

    assert revived.snapshot() == {"alice": 1001}
    assert revived.authorize(user(2002, "bob")) is False

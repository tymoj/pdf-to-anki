from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from pdf_to_anki.bot.auth import Authorizer
from pdf_to_anki.bot.pins import PINS_KEY, load_pins, save_pins
from pdf_to_anki.storage.s3 import StorageError


def user(uid: int, handle: str | None):
    return SimpleNamespace(id=uid, username=handle)


def test_pin_is_persisted_on_first_contact():
    storage = MagicMock()
    auth = Authorizer(frozenset({"alice"}), on_pin=lambda s: save_pins(storage, s))

    assert auth.authorize(user(42, "alice")) is True

    storage.put_bytes.assert_called_once()
    key, payload = storage.put_bytes.call_args.args[:2]
    assert key == PINS_KEY
    assert json.loads(payload.decode()) == {"alice": 42}


def test_no_write_when_no_new_pin_is_created():
    storage = MagicMock()
    auth = Authorizer(frozenset({"alice"}), on_pin=lambda s: save_pins(storage, s))
    auth.authorize(user(42, "alice"))
    storage.put_bytes.reset_mock()

    auth.authorize(user(42, "alice"))  # already pinned
    auth.authorize(user(7, "mallory"))  # not allowed

    storage.put_bytes.assert_not_called()


def test_restored_pins_survive_a_restart_and_refuse_a_takeover():
    storage = MagicMock()
    storage.exists.return_value = True
    storage.get_bytes.return_value = json.dumps({"alice": 42}).encode()

    auth = Authorizer(frozenset({"alice"}), pins=load_pins(storage))

    # The window a restart used to re-open.
    assert auth.authorize(user(999, "alice")) is False
    # The original owner keeps access even after renaming.
    assert auth.authorize(user(42, "renamed")) is True


def test_missing_pin_object_starts_empty():
    storage = MagicMock()
    storage.exists.return_value = False
    assert load_pins(storage) == {}
    storage.get_bytes.assert_not_called()


def test_unreadable_pins_degrade_to_empty_rather_than_crashing(caplog):
    storage = MagicMock()
    storage.exists.return_value = True
    storage.get_bytes.return_value = b"{not json"

    assert load_pins(storage) == {}
    assert "could not load auth pins" in caplog.text


def test_storage_failure_while_saving_does_not_break_authorization(caplog):
    storage = MagicMock()
    storage.put_bytes.side_effect = StorageError("minio down")
    auth = Authorizer(frozenset({"alice"}), on_pin=lambda s: save_pins(storage, s))

    # A storage outage must not lock out a legitimate user.
    assert auth.authorize(user(42, "alice")) is True
    assert "could not persist auth pins" in caplog.text

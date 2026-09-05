"""Persistence for the auth pin map.

Pins live in memory in ``Authorizer``; without this they are lost on restart and
first-contact resolution reopens, which is precisely the window a freed-handle
hijack needs. Stored as one small JSON object in the same bucket as the decks.
"""

from __future__ import annotations

import json
import logging

from ..storage.s3 import Storage, StorageError

PINS_KEY = "auth/pins.json"

logger = logging.getLogger(__name__)


def load_pins(storage: Storage) -> dict[str, int]:
    try:
        if not storage.exists(PINS_KEY):
            return {}
        raw = json.loads(storage.get_bytes(PINS_KEY).decode("utf-8"))
    except (StorageError, ValueError, UnicodeDecodeError) as exc:
        # Starting with an empty map is safe: it only re-opens first-contact
        # resolution, never grants access to a handle that isn't on the allowlist.
        logger.warning("could not load auth pins (%s); starting with none", exc)
        return {}
    return {str(k): int(v) for k, v in raw.items() if str(k) and v is not None}


def save_pins(storage: Storage, pins: dict[str, int]) -> None:
    try:
        storage.put_bytes(PINS_KEY, json.dumps(pins, sort_keys=True).encode("utf-8"))
    except StorageError as exc:
        logger.warning("could not persist auth pins (%s)", exc)

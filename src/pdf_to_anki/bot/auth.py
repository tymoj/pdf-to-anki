"""Access control for the Telegram bot.

The allowlist is configured as Telegram *handles* (``TELEGRAM_ALLOWED_USERNAMES``),
but handles are mutable and re-registerable: once someone releases ``@alice``,
anyone may claim it. A literal handle check would hand that person access.

So a handle is only ever used once, to *resolve* an allowed name to the numeric
``user.id`` behind it on first contact. That id is then pinned, and from then on
authorization is decided by id alone. Consequences, both intentional:

* an allowed user who later renames keeps access (the id still matches);
* a stranger who claims a freed allowed handle is refused, because the handle is
  already pinned to a different id.

Pins live in memory. A caller may persist :meth:`Authorizer.snapshot` and feed it
back through :meth:`Authorizer.restore`; if the pins are lost, every allowed
handle simply reverts to being unresolved and first contact resolves it again.
This module deliberately does no I/O so it stays trivially testable.
"""

from __future__ import annotations

import logging
from typing import Callable, Mapping, Protocol

logger = logging.getLogger(__name__)


class TelegramUser(Protocol):
    """Structural stand-in for :class:`telegram.User` (id plus optional handle)."""

    id: int
    username: str | None


class Authorizer:
    def __init__(
        self,
        allowed_usernames: frozenset[str] | set[str],
        pins: Mapping[str, int] | None = None,
        on_pin: Callable[[dict[str, int]], None] | None = None,
    ) -> None:
        self._allowed = frozenset(u.lstrip("@").lower() for u in allowed_usernames)
        self._pins: dict[str, int] = {}
        # Fires only when a new pin is created, so a caller can persist it without
        # writing storage on every authorized message.
        self._on_pin = on_pin
        if pins:
            self.restore(pins)

    @property
    def allowed_usernames(self) -> frozenset[str]:
        return self._allowed

    def authorize(self, user: TelegramUser | None) -> bool:
        """Return whether ``user`` may use the bot, pinning them on first contact."""
        if user is None:
            logger.warning("auth denied: update carried no user")
            return False

        handle = (user.username or "").lstrip("@").lower() or None

        pinned_as = self._pinned_username_for(user.id)
        if pinned_as is not None:
            return True

        if handle is None:
            logger.warning("auth denied: user id=%s has no username and is not pinned", user.id)
            return False

        if handle not in self._allowed:
            logger.warning("auth denied: user id=%s handle=@%s not on the allowlist", user.id, handle)
            return False

        owner_id = self._pins.get(handle)
        if owner_id is None:
            self._pins[handle] = user.id
            logger.info("auth pinned: handle @%s resolved to user id=%s", handle, user.id)
            if self._on_pin is not None:
                self._on_pin(dict(self._pins))
            return True

        # The allowed handle is already spoken for by someone else: either a handle
        # takeover or an impersonation attempt. Never silently re-pin.
        logger.warning(
            "auth denied: handle @%s is pinned to user id=%s but was presented by user id=%s",
            handle,
            owner_id,
            user.id,
        )
        return False

    def snapshot(self) -> dict[str, int]:
        """Current ``username -> user_id`` pins, for a caller to persist."""
        return dict(self._pins)

    def restore(self, mapping: Mapping[str, int]) -> None:
        """Load previously persisted pins, ignoring handles no longer allowed."""
        for username, user_id in mapping.items():
            handle = username.lstrip("@").lower()
            if handle in self._allowed:
                self._pins[handle] = int(user_id)
            else:
                logger.info("auth restore: dropping pin for @%s, no longer allowed", handle)

    def _pinned_username_for(self, user_id: int) -> str | None:
        for username, pinned_id in self._pins.items():
            if pinned_id == user_id:
                return username
        return None

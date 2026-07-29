"""Who is online, as a count.

The client asks the server something every couple of seconds while anyone is
looking at it. That is a heartbeat already; this only writes down when it last
happened.

Kept in memory on purpose. Presence is true for a minute at a time, and a
restart should forget it — a stored "online" list would come back after a
redeploy claiming people are there who closed the client hours ago.
"""
from __future__ import annotations

import time


class Presence:
    """Last-seen stamps, and the two questions asked of them."""

    #: How long a stamp counts as "still here". Comfortably more than the
    #: slowest client poll (3s idle) plus a network hiccup, and short enough
    #: that somebody who closed the client disappears while you are still
    #: looking at the screen.
    WINDOW_S = 45.0

    def __init__(self, now=time.time) -> None:
        self._now = now
        self.last_seen: dict[str, float] = {}

    def seen(self, account_id: str) -> None:
        self.last_seen[account_id] = self._now()

    def gone(self, account_id: str) -> None:
        """Forget somebody deliberately — a logout, not a timeout."""
        self.last_seen.pop(account_id, None)

    def is_online(self, account_id: str) -> bool:
        return self._now() - self.last_seen.get(account_id, 0.0) <= self.WINDOW_S

    def online(self) -> int:
        """How many clients are currently around.

        A count, never a list: people agreed to have their *matches* tracked,
        which is not the same as publishing when they are at their computer.
        """
        cutoff = self._now() - self.WINDOW_S
        # Swept as it is read, or the dictionary grows for every account that
        # ever logged in.
        self.last_seen = {k: v for k, v in self.last_seen.items() if v > cutoff}
        return len(self.last_seen)

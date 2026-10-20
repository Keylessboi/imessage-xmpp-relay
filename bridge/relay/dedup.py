"""Bounded LRU of already-delivered message guids.

The SSE stream can replay events (reconnect, upstream restart, at-least-once
delivery), so every message event is filtered through this cache before it is
turned into an XMPP stanza.
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Hashable, Optional


class DedupCache:
    """A fixed-capacity least-recently-used set.

    check_and_add(key) returns True the first time a key is seen and False
    afterwards. Keys are evicted in insertion order once capacity is reached, so
    memory stays bounded for a long-running process.
    """

    def __init__(self, maxsize: int = 4096) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self.maxsize = maxsize
        self._seen: "OrderedDict[Hashable, None]" = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def __contains__(self, key: Hashable) -> bool:
        return key in self._seen

    def check_and_add(self, key: Optional[Hashable]) -> bool:
        """Return True if key is new (and remember it); False if it is a replay."""
        if key is None or key == "":
            # A message without a guid cannot be deduplicated; deliver it.
            return True
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = None
        if len(self._seen) > self.maxsize:
            self._seen.popitem(last=False)
        return True

    def clear(self) -> None:
        self._seen.clear()

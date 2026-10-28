"""Thread-safe request identity tracking, independent of GTK and clocks."""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestToken:
    request_id: int
    context: str
    generation: int


class RequestRegistry:
    """Track the newest live request token in each independent context."""

    def __init__(self):
        self._lock = threading.Lock()
        self._next_id = 0
        self._current: dict[str, RequestToken] = {}

    def begin(self, context: str, generation: int = 0) -> RequestToken:
        with self._lock:
            self._next_id += 1
            token = RequestToken(self._next_id, context, generation)
            self._current[context] = token
            return token

    def is_current(self, token: RequestToken) -> bool:
        with self._lock:
            return self._current.get(token.context) == token

    def finish(self, token: RequestToken) -> None:
        with self._lock:
            if self._current.get(token.context) == token:
                del self._current[token.context]

    def invalidate(self, context: str | None = None) -> None:
        with self._lock:
            if context is None:
                self._current.clear()
            else:
                self._current.pop(context, None)

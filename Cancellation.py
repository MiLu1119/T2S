"""Thread-safe ownership-aware registry for cooperative query cancellation."""

from __future__ import annotations

import threading


class CancellationRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, threading.Event]] = {}

    def create(self, request_id: str, user_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._items[request_id] = (user_id, event)
        return event

    def cancel(self, request_id: str, user_id: str, *, is_admin: bool = False) -> bool:
        with self._lock:
            item = self._items.get(request_id)
            if item is None:
                return False
            owner_id, event = item
            if owner_id != user_id and not is_admin:
                raise PermissionError("request belongs to another user")
            event.set()
            return True

    def remove(self, request_id: str) -> None:
        with self._lock:
            self._items.pop(request_id, None)

    def is_active(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self._items

    def get_event(self, request_id: str) -> threading.Event | None:
        with self._lock:
            item = self._items.get(request_id)
            return item[1] if item else None


# Runtime control objects stay process-local; LangGraph state carries only request_id.
cancellations = CancellationRegistry()

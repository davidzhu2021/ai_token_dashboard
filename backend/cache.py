import time
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheEntry:
    value: Any
    expires_at: float


class TTLCache:
    def __init__(self) -> None:
        self._items: dict[str, CacheEntry] = {}

    def get(self, key: str) -> tuple[bool, Any, int]:
        now = time.time()
        entry = self._items.get(key)
        if not entry:
            return False, None, 0
        if entry.expires_at <= now:
            self._items.pop(key, None)
            return False, None, 0
        return True, entry.value, max(0, int(entry.expires_at - now))

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        self._items[key] = CacheEntry(value=value, expires_at=time.time() + ttl_seconds)

    def delete(self, key: str) -> None:
        self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()

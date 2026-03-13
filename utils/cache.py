"""Thread-safe TTL cache utility."""

import threading
import time


class TTLCache:
    """In-memory cache with time-to-live (TTL) expiration.

    Thread-safe. Uses monotonic clock for TTL. Returns (hit, value) tuples
    to distinguish between a cached None and a cache miss.
    Evicts earliest-expiry entries when maxsize is exceeded.
    """

    def __init__(self, ttl: float, maxsize: int = 256):
        self._ttl = ttl
        self._maxsize = maxsize
        self._store: dict = {}  # key -> (value, expires_at)
        self._lock = threading.Lock()

    def get(self, key) -> tuple[bool, object]:
        """Return (True, value) if key is cached and not expired, else (False, None)."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return (False, None)
            value, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._store[key]
                return (False, None)
            return (True, value)

    def set(self, key, value) -> None:
        """Store value with TTL. Evicts oldest entry if maxsize exceeded."""
        expires_at = time.monotonic() + self._ttl
        with self._lock:
            if key not in self._store and len(self._store) >= self._maxsize:
                # Evict entry with earliest expiry
                evict_key = min(self._store, key=lambda k: self._store[k][1])
                del self._store[evict_key]
            self._store[key] = (value, expires_at)

    def delete(self, key) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

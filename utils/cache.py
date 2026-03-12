"""Thread-safe TTL cache utility."""

import threading
import time
from typing import Any


class TTLCache:
    """Thread-safe in-memory cache with TTL expiry and earliest-expiry eviction.

    Designed for module-level (global) usage so cache persists across
    multiple scorer instances created within the same process.

    Note: Eviction policy is earliest-expiry (not LRU). Suitable for
    financial data where all entries age at a similar rate.
    Keep maxsize <= 512 for O(n) eviction to remain acceptable.

    Example:
        _my_cache = TTLCache(default_ttl=3600, maxsize=256)

        hit, val = _my_cache.get("key")
        if not hit:
            val = expensive_call()
            _my_cache.set("key", val)
    """

    def __init__(self, default_ttl: int, maxsize: int = 256):
        """
        Args:
            default_ttl: Default TTL in seconds.
            maxsize: Max number of entries before earliest-expiry eviction.
        """
        self._store: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)
        self._ttl = default_ttl
        self._maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bool, Any]:
        """Return (hit, value). Returns (False, None) on miss or expiry."""
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                value, expires_at = entry
                if time.monotonic() < expires_at:
                    return True, value
                del self._store[key]
            return False, None

    def set(self, key: str, value: Any, ttl: int = None) -> None:
        """Store value with given TTL (falls back to default_ttl)."""
        expires_at = time.monotonic() + (ttl if ttl is not None else self._ttl)
        with self._lock:
            if len(self._store) >= self._maxsize and key not in self._store:
                # Earliest-expiry eviction
                oldest = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest]
            self._store[key] = (value, expires_at)

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

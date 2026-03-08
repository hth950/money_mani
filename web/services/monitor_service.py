"""Real-time monitor management + SSE bridge."""
import asyncio
import json
import logging
import threading
from typing import AsyncGenerator

logger = logging.getLogger("money_mani.web.services.monitor")


class MonitorService:
    """Manage RealtimeMonitor in a background thread with SSE event queue."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._monitor = None
        self._thread = None
        self._queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    def _on_signal(self, signal_info: dict):
        """Callback from RealtimeMonitor (runs in monitor thread)."""
        if self._queue and self._loop:
            try:
                self._loop.call_soon_threadsafe(
                    self._queue.put_nowait, signal_info
                )
            except Exception as e:
                logger.warning(f"Failed to enqueue signal: {e}")

    def start(self, market_filter: str = None) -> dict:
        """Start the monitor in a background thread."""
        if self._running:
            return {"status": "already_running"}

        from monitor.realtime_monitor import RealtimeMonitor
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._monitor = RealtimeMonitor(
            market_filter=market_filter,
            on_signal=self._on_signal,
        )
        self._running = True

        def _run():
            try:
                self._monitor.start()
            except Exception as e:
                logger.error(f"Monitor thread error: {e}")
            finally:
                self._running = False

        self._thread = threading.Thread(target=_run, daemon=True, name="realtime-monitor")
        self._thread.start()
        return {"status": "started", "market_filter": market_filter}

    def stop(self) -> dict:
        """Stop the monitor."""
        if not self._running or not self._monitor:
            return {"status": "not_running"}
        self._monitor.stop()
        self._running = False
        return {"status": "stopped"}

    def is_running(self) -> bool:
        return self._running

    async def event_stream(self) -> AsyncGenerator[str, None]:
        """SSE event generator. Yields signal events as SSE format."""
        if not self._queue:
            self._queue = asyncio.Queue()

        # Send initial connected event
        yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"

        while True:
            try:
                signal = await asyncio.wait_for(self._queue.get(), timeout=30.0)
                data = json.dumps(signal, ensure_ascii=False, default=str)
                yield f"event: signal\ndata: {data}\n\n"
            except asyncio.TimeoutError:
                # Send keepalive
                yield f"event: keepalive\ndata: {json.dumps({'type': 'keepalive'})}\n\n"
            except Exception as e:
                logger.error(f"SSE stream error: {e}")
                break

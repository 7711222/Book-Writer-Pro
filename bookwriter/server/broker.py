"""In-memory event broker for the write-job SSE stream.

The pipeline runs in a background daemon thread and emits structured ``on_event``
dicts. Those are published here from the worker thread; SSE subscribers (async
generators on the event loop) drain per-subscriber ``asyncio.Queue`` objects.

Design points the API contract requires:
  * per-book bounded ring buffer so *late subscribers* replay everything emitted
    for the current/last job, then tail live events;
  * thread-safe publish: the worker thread never touches asyncio objects directly
    — it hands each event to the loop via ``loop.call_soon_threadsafe``;
  * running-job tracking so the API can return 409 when a job is already active.

Each event stream is terminated by a sentinel ``{"type": "done"}`` (or
``{"type": "error", "message": ...}``); the broker records that the book's job
has finished but keeps the ring buffer so a viewer arriving afterwards still sees
the full last run.
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

# A terminal event of this type ends a job's stream.
TERMINAL_TYPES = {"done", "error"}

# (sequence id, event) — the seq is monotonically increasing per book (never
# reset, even across jobs) so SSE ``id:`` fields / ``Last-Event-ID`` reconnects
# can tell "already seen" from "new" without ambiguity.
SeqEvent = Tuple[int, Dict[str, Any]]


class _BookChannel:
    def __init__(self, ring_size: int) -> None:
        self.ring: Deque[SeqEvent] = deque(maxlen=ring_size)
        self.subscribers: Set["asyncio.Queue[SeqEvent]"] = set()
        self.running: bool = False
        self.finished: bool = True  # True once a terminal event has been seen
        self.next_seq: int = 0  # per-book monotonic event counter


class EventBroker:
    """Thread-safe fan-out of pipeline events to async SSE subscribers."""

    def __init__(self, ring_size: int = 4000) -> None:
        self._ring_size = ring_size
        self._channels: Dict[str, _BookChannel] = {}
        self._lock = threading.Lock()
        # The asyncio loop the API runs on; set by the API at startup so worker
        # threads can schedule queue puts thread-safely.
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- loop wiring ----------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _ensure_loop(self) -> None:
        """Capture the current running event loop.

        Called from async request handlers (subscribe), so it always runs on the
        server's loop. Refreshed on EVERY subscribe: under a real single-loop
        server this is a no-op re-assignment, while under Starlette's TestClient
        (where each request may run on a fresh portal loop) it keeps publishes
        pointed at a live loop instead of one that closed with an earlier
        request. This makes loop wiring robust whether or not the ASGI startup
        lifespan fired (e.g. under httpx ASGITransport it does not).
        """
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

    def _channel(self, book_id: str) -> _BookChannel:
        ch = self._channels.get(book_id)
        if ch is None:
            ch = _BookChannel(self._ring_size)
            self._channels[book_id] = ch
        return ch

    # -- job lifecycle --------------------------------------------------
    def is_running(self, book_id: str) -> bool:
        with self._lock:
            ch = self._channels.get(book_id)
            return bool(ch and ch.running)

    def start_job(self, book_id: str) -> bool:
        """Mark a job as running. Returns False if one is already running.

        Clears the previous run's ring buffer so the new job's replay is clean.
        """
        with self._lock:
            ch = self._channel(book_id)
            if ch.running:
                return False
            ch.ring.clear()
            ch.running = True
            ch.finished = False
            return True

    # -- publish (called from the worker thread) ------------------------
    def publish(self, book_id: str, event: Dict[str, Any]) -> None:
        """Append to the ring and fan out to live subscribers.

        Safe to call from any thread. asyncio.Queue puts are marshalled onto the
        event loop via call_soon_threadsafe.
        """
        with self._lock:
            ch = self._channel(book_id)
            seq = ch.next_seq
            ch.next_seq += 1
            ch.ring.append((seq, event))
            if event.get("type") in TERMINAL_TYPES:
                ch.running = False
                ch.finished = True
            subscribers = list(ch.subscribers)
        loop = self._loop
        if loop is None:
            return
        for q in subscribers:
            try:
                loop.call_soon_threadsafe(_safe_put, q, (seq, event))
            except RuntimeError:
                # The captured loop already closed (possible under TestClient
                # teardown). The ring still holds the event for replay, and the
                # publishing worker thread must never die over fan-out.
                pass

    # -- subscribe (called from the SSE async generator) ----------------
    def subscribe(self, book_id: str) -> "tuple[asyncio.Queue[SeqEvent], List[SeqEvent], bool]":
        """Register a subscriber.

        Returns (queue, replay_events, already_finished); events are
        ``(seq, event)`` pairs. The caller should emit every event in
        ``replay_events`` first, then drain the queue for live events. If
        ``already_finished`` is True and the replay already contains a terminal
        event, the caller can close after replay.
        """
        self._ensure_loop()
        q: "asyncio.Queue[SeqEvent]" = asyncio.Queue()
        with self._lock:
            ch = self._channel(book_id)
            replay = list(ch.ring)
            finished = ch.finished
            ch.subscribers.add(q)
        return q, replay, finished

    def unsubscribe(self, book_id: str, q: "asyncio.Queue[SeqEvent]") -> None:
        with self._lock:
            ch = self._channels.get(book_id)
            if ch is not None:
                ch.subscribers.discard(q)

    def drop(self, book_id: str) -> None:
        """Forget a book's channel entirely (ring + subscriber set).

        Called when a book is deleted so its (potentially megabyte-sized) ring
        of full-text events does not leak for the process lifetime. If a job is
        still running or subscribers remain, we leave the channel in place — the
        caller (delete) refuses to delete a running book, so this is just a
        guard against tearing a live stream out from under a viewer.
        """
        with self._lock:
            ch = self._channels.get(book_id)
            if ch is None:
                return
            if ch.running or ch.subscribers:
                # Keep it; clear the ring to release the bulk of the memory.
                ch.ring.clear()
                return
            ch.ring.clear()
            del self._channels[book_id]


def _safe_put(q: "asyncio.Queue[SeqEvent]", event: SeqEvent) -> None:
    try:
        q.put_nowait(event)
    except Exception:
        # Unbounded queue; put_nowait effectively never fails, but never let a
        # subscriber problem crash the publishing path.
        pass

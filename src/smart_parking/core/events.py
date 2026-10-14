"""Event bus: the seam between the AI pipeline and the business logic.

An in-process implementation is enough for one camera. Redis Streams can be
dropped in later behind the same ``EventBus`` protocol without touching
either side.
"""

from __future__ import annotations

import queue
import threading
from typing import Callable, Iterable, Protocol, runtime_checkable

from smart_parking.core.schemas import Event

Subscriber = Callable[[Event], None]


@runtime_checkable
class EventBus(Protocol):
    def publish(self, event: Event) -> None: ...

    def subscribe(self, handler: Subscriber) -> Callable[[Event], None]: ...

    def drain(self, timeout: float = 0.0) -> list[Event]: ...


class InMemoryEventBus:
    """Thread-safe fan-out bus with a bounded buffer.

    The pipeline thread publishes; consumers drain from their own loop. If the
    buffer fills we drop the oldest event rather than blocking inference -
    losing a slot event is recoverable from the FSM's next transition, while
    stalling the grabber would let latency accumulate.
    """

    def __init__(self, maxsize: int = 10_000) -> None:
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
        self._handlers: list[Subscriber] = []
        self._lock = threading.Lock()
        self.dropped = 0

    def publish(self, event: Event) -> None:
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # a bad subscriber must not kill inference
                pass
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except queue.Empty:
                pass

    def subscribe(self, handler: Subscriber) -> Callable[[Event], None]:
        with self._lock:
            self._handlers.append(handler)
        return handler

    def unsubscribe(self, handler: Subscriber) -> None:
        with self._lock:
            self._handlers = [h for h in self._handlers if h is not handler]

    def drain(self, timeout: float = 0.0) -> list[Event]:
        out: list[Event] = []
        first = _get_with_timeout(self._queue, timeout)
        if first is not None:
            out.append(first)
        while True:
            item = _get_with_timeout(self._queue, 0.0)
            if item is None:
                break
            out.append(item)
        return out

    def __len__(self) -> int:
        return self._queue.qsize()


def _get_with_timeout(q: queue.Queue[Event], timeout: float) -> Event | None:
    if timeout <= 0:
        try:
            return q.get_nowait()
        except queue.Empty:
            return None
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


class RecordingEventBus(InMemoryEventBus):
    """Keeps every event in a list as well, for tests and replay."""

    def __init__(self) -> None:
        super().__init__()
        self.recorded: list[Event] = []

    def publish(self, event: Event) -> None:
        self.recorded.append(event)
        super().publish(event)

    def by_type(self, *types: str) -> Iterable[Event]:
        return [e for e in self.recorded if not types or e.type in types]

"""Abort active HTTP reads when the caller's total execution budget expires."""

from __future__ import annotations

from collections.abc import Callable
from threading import Event, Lock, Timer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.runtime.core.context import AgentRuntime

from .errors import ModelTransportError

AbortRegistrar = Callable[[Callable[[], None]], Callable[[], None]]


class RequestDeadline:
    def __init__(
        self,
        remaining: float | None,
        cancelled: Callable[[], bool],
        register_abort: AbortRegistrar | None,
    ) -> None:
        self._cancelled = cancelled
        self._parent_register = register_abort
        self._expired = Event()
        self._lock = Lock()
        self._aborters: dict[Callable[[], None], Callable[[], None]] = {}
        self._timer = Timer(max(0, remaining), self._expire) if remaining is not None else None

    def start(self) -> None:
        if self._timer is not None:
            self._timer.daemon = True
            self._timer.start()

    def interrupted(self) -> bool:
        return self._expired.is_set() or self._cancelled()

    def register_abort(self, callback: Callable[[], None]) -> Callable[[], None]:
        called = Event()
        call_lock = Lock()

        def once() -> None:
            with call_lock:
                if called.is_set():
                    return
                called.set()
            callback()

        remove_parent = self._parent_register(once) if self._parent_register is not None else lambda: None
        with self._lock:
            self._aborters[once] = remove_parent
            expired = self._expired.is_set()
        if expired:
            once()

        def unregister() -> None:
            with self._lock:
                self._aborters.pop(once, None)
            remove_parent()

        return unregister

    def _expire(self) -> None:
        with self._lock:
            self._expired.set()
            callbacks = tuple(self._aborters)
        for abort in callbacks:
            try:
                abort()
            except Exception:
                # A closed socket must not prevent other active reads stopping.
                continue

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        with self._lock:
            removers = tuple(self._aborters.values())
            self._aborters.clear()
        for remove in removers:
            remove()


def execution_remaining(runtime: AgentRuntime) -> float | None:
    remaining = runtime.services.execution_time_remaining
    value = remaining() if remaining is not None else None
    if value is not None and value <= 0:
        raise ModelTransportError("Agent time budget exhausted.", retryable=False)
    return value

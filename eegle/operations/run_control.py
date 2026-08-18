"""Thread-safe control for one operations-owned execution engine."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Condition

from eegle._validation import require_identifier
from eegle.runtime import ExecutionEngine


@dataclass(frozen=True, slots=True)
class RunControlSnapshot:
    """Read-only lifecycle state safe to expose to a process supervisor."""

    attached: bool
    finished: bool
    action: str | None = None
    reason: str | None = None


class RunControl:
    """Queue one completion/cancellation request without exposing the engine.

    The first terminal request wins. Requests made before the operations layer
    constructs the engine are retained and applied immediately on attachment.
    """

    def __init__(self) -> None:
        self._condition = Condition()
        self._engine: ExecutionEngine | None = None
        self._attached_once = False
        self._action: str | None = None
        self._reason: str | None = None
        self._finished = False

    @property
    def snapshot(self) -> RunControlSnapshot:
        with self._condition:
            return RunControlSnapshot(
                attached=self._attached_once,
                finished=self._finished,
                action=self._action,
                reason=self._reason,
            )

    def wait_until_attached(self, timeout_seconds: float | None = None) -> bool:
        """Wait for engine attachment, returning false if the run ends first."""

        timeout = None if timeout_seconds is None else float(timeout_seconds)
        if timeout is not None and timeout < 0:
            raise ValueError("timeout_seconds cannot be negative")
        with self._condition:
            self._condition.wait_for(
                lambda: self._attached_once or self._finished,
                timeout=timeout,
            )
            return self._attached_once

    def complete(self, reason: str = "completion_requested") -> bool:
        """Request orderly completion after draining already admitted work."""

        return self._request("complete", reason)

    def cancel(self, reason: str = "cancel_requested") -> bool:
        """Request prompt cancellation and partial-session finalization."""

        return self._request("cancel", reason)

    def _request(self, action: str, reason: str) -> bool:
        normalized_reason = require_identifier(reason, "run control reason")
        engine: ExecutionEngine | None
        with self._condition:
            if self._action is not None or self._finished:
                return False
            self._action = action
            self._reason = normalized_reason
            engine = self._engine
            self._condition.notify_all()
        if engine is not None:
            if action == "complete":
                engine.complete(normalized_reason)
            else:
                engine.cancel(normalized_reason)
        return True

    def _attach(self, engine: ExecutionEngine) -> None:
        if not isinstance(engine, ExecutionEngine):
            raise TypeError("RunControl can attach only to an ExecutionEngine")
        action: str | None
        reason: str | None
        with self._condition:
            if self._engine is not None:
                raise RuntimeError("RunControl is already attached")
            if self._finished:
                raise RuntimeError("RunControl belongs to a finished run")
            self._engine = engine
            self._attached_once = True
            action = self._action
            reason = self._reason
            self._condition.notify_all()
        if action == "complete" and reason is not None:
            engine.complete(reason)
        elif action == "cancel" and reason is not None:
            engine.cancel(reason)

    def _finish(self) -> None:
        with self._condition:
            self._engine = None
            self._finished = True
            self._condition.notify_all()

"""An optional agent-only deadline; preparation and grading have other limits."""

from __future__ import annotations

import json
import math
from time import monotonic

from backend.tools.base import ToolError


class AgentTimeBudget:
    def __init__(self, timeout_seconds: float | None = None, reserve_seconds: float = 60) -> None:
        if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("Agent timeout must be a positive finite number.")
        if not math.isfinite(reserve_seconds) or reserve_seconds < 0:
            raise ValueError("Finalization reserve must be finite and non-negative.")
        self.timeout_seconds = timeout_seconds
        self.reserve_seconds = min(reserve_seconds, timeout_seconds / 2) if timeout_seconds is not None else 0
        self.deadline: float | None = None

    def start(self) -> None:
        self.deadline = monotonic() + self.timeout_seconds if self.timeout_seconds is not None else None

    def remaining(self) -> float | None:
        return max(0.0, self.deadline - monotonic()) if self.deadline is not None else None

    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0

    def command_timeout(self, requested: float) -> float:
        remaining = self.remaining()
        if remaining is None:
            return requested
        available = remaining - self.reserve_seconds
        if available <= 0:
            error = ToolError(
                "Finalization reserve reached. Do not start new tools; report results and unresolved checks."
            )
            error.tool_output = json.dumps({"not_started": True, "output": str(error)})
            raise error
        return min(requested, available)

"""Privacy-conscious JSONL tracing for GitHub Agent runs."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SENSITIVE_KEYS = {"authorization", "content", "password", "secret", "token", "api_key"}
_MAX_STRING_LENGTH = 240


class TraceRecorder:
    """Persist sanitized API and agent-step metadata for one CLI ask run."""

    def __init__(self, directory: Path, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.run_id}.jsonl"

    def record(self, event: str, **data: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "event": event,
            **_sanitize(data),
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def api_event(self, event: str, data: dict[str, Any]) -> None:
        """Adapter used by GitHubClient without exposing response bodies."""
        self.record(event, **data)

    def record_agent_step(self, step: object) -> None:
        """Record tool names, sanitized arguments, error state, and duration only."""
        tool_calls = []
        for tool_call in getattr(step, "tool_calls", []) or []:
            tool_calls.append(
                {
                    "name": getattr(tool_call, "name", "unknown"),
                    "arguments": getattr(tool_call, "arguments", {}) or {},
                }
            )

        timing = getattr(step, "timing", None)
        started = getattr(timing, "start_time", None)
        ended = getattr(timing, "end_time", None)
        duration_ms = round((ended - started) * 1000) if started is not None and ended is not None else None
        error = getattr(step, "error", None)
        self.record(
            "agent_step",
            step_number=getattr(step, "step_number", None),
            tools=tool_calls,
            is_final_answer=bool(getattr(step, "is_final_answer", False)),
            error_type=type(error).__name__ if error else None,
            duration_ms=duration_ms,
        )


def _sanitize(value: Any, key: str = "") -> Any:
    """Remove secrets and cap unbounded text before it reaches a trace file."""
    if key.lower() in _SENSITIVE_KEYS:
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING_LENGTH else value[:_MAX_STRING_LENGTH] + "…[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"[{type(value).__name__}]"

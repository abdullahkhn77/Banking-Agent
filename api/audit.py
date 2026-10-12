"""Structured JSON audit log: one line per event, PAN-redacted, never carrying message text or other customers' data.

Events: session_start, user_message (length only), tool_call, confirmation, execution, reply (length only),
session_end. Every line carries ts, request_id, session_id, user_id. The sink is stdout by default or a file path.
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from agent.graph import redact_pans


def _redact(value: Any) -> Any:
    """PAN-redact every string anywhere in the value (recursively)."""
    if isinstance(value, str):
        return redact_pans(value)
    if isinstance(value, Mapping):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


class AuditLog:
    def __init__(self, sink: TextIO | None = None, path: str | Path | None = None) -> None:
        self._own = False
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._sink: TextIO = Path(path).open("a", encoding="utf-8")
            self._own = True
        else:
            self._sink = sink or sys.stdout
        self._lock = threading.Lock()

    def emit(self, event_type: str, request_id: str, session_id: str, user_id: str, **fields: Any) -> dict[str, Any]:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event_type,
            "request_id": request_id,
            "session_id": session_id,
            "user_id": user_id,
            **_redact(fields),
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self._sink.write(line + "\n")
            self._sink.flush()
        return record

    def close(self) -> None:
        if self._own:
            self._sink.close()


def audit_trace_event(log: AuditLog, request_id: str, session_id: str, user_id: str, event: dict[str, Any]) -> None:
    """Translate one graph trace event into an audit line (validated args for tool_call; lengths, never content)."""
    kind = event.get("type")
    base = (request_id, session_id, user_id)
    if kind == "tool_call":
        log.emit(
            "tool_call",
            *base,
            tool=event.get("name"),
            decision=event.get("decision"),
            layer=event.get("layer"),
            code=event.get("code"),
            args=event.get("args"),
        )
    elif kind == "confirmation":
        log.emit("confirmation", *base, requested=event.get("requested"), approved=event.get("approved"))
    elif kind == "execution":
        log.emit(
            "execution",
            *base,
            tool=event.get("name"),
            ok=event.get("ok"),
            latency_s=round(event.get("latency_s", 0.0), 4),
            retries=event.get("retries", 0),
            error=event.get("error"),
        )

"""Temporary runtime debug logging for Cursor debug session d49c0a."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

_LOG_PATH = "/Users/alexalmond/ws/wayonagio-email-agent/.cursor/debug-d49c0a.log"
_SESSION_ID = "d49c0a"


def write_debug_log(
    *,
    run_id: str,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any],
) -> None:
    payload = {
        "sessionId": _SESSION_ID,
        "id": f"log_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        "timestamp": int(time.time() * 1000),
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
    }
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str, separators=(",", ":")) + "\n")
    except OSError:
        pass

"""Agent debug logging (NDJSON) — session 406e85."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "debug-406e85.log"
_SESSION = "406e85"


def agent_log(
    location: str,
    message: str,
    data: dict[str, Any] | None = None,
    *,
    hypothesis_id: str | None = None,
    run_id: str | None = None,
) -> None:
    # #region agent log
    try:
        payload: dict[str, Any] = {
            "sessionId": _SESSION,
            "timestamp": int(time.time() * 1000),
            "location": location,
            "message": message,
            "data": data or {},
        }
        if hypothesis_id:
            payload["hypothesisId"] = hypothesis_id
        if run_id:
            payload["runId"] = run_id
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass
    # #endregion

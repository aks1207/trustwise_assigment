"""Append-only JSONL storage for audit records, and helpers to compare records across runs."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

# Fields that legitimately differ between two runs of the same evaluation.
VOLATILE_KEYS = frozenset({
    "request_id", "call_id", "evaluated_at", "started_at", "completed_at", "latency_ms", "total_latency_ms",
})


class AuditLog:
    """Appends one JSON object per line and fsyncs. Never truncates or rewrites an existing file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock, open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


def strip_volatile(obj: Any) -> Any:
    """Copy with ids, timestamps and latencies removed, for comparing two runs."""
    if isinstance(obj, dict):
        return {k: strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [strip_volatile(v) for v in obj]
    return obj

"""Turn raw judge text into a validated verdict, or say exactly why not.

Tolerated: a markdown fence or prose around a single JSON object, a lowercase verdict word.
Not tolerated: missing or extra keys, a confidence outside [0, 1] (no clamping), a boolean or string
confidence, a non-string reasoning. Those are parse failures, which the engine retries.
Reasoning length is not limited for now (open item, see docs/POLICY_DESIGN.md).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

VERDICTS = ("PASS", "FAIL", "UNCERTAIN")
REQUIRED_KEYS = ("verdict", "confidence", "reasoning")


@dataclass(frozen=True)
class ParseResult:
    status: str                  # ok | invalid_json | missing_fields | invalid_values | extra_fields
    parsed: dict[str, Any] | None
    message: str


def parse_judge_response(raw: str) -> ParseResult:
    obj = _extract_object(raw)
    if obj is None:
        return ParseResult("invalid_json", None, "expected exactly one JSON object in judge reply")

    missing = [k for k in REQUIRED_KEYS if k not in obj]
    if missing:
        return ParseResult("missing_fields", None, f"missing fields: {', '.join(missing)}")
    extra = sorted(set(obj) - set(REQUIRED_KEYS))
    if extra:
        return ParseResult("extra_fields", None, f"unexpected fields: {', '.join(extra)}")

    v = obj["verdict"]
    if not isinstance(v, str) or v.strip().upper() not in VERDICTS:
        return ParseResult("invalid_values", None, f"verdict must be one of {VERDICTS}, got {v!r}")
    c = obj["confidence"]
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or not 0 <= c <= 1:
        return ParseResult("invalid_values", None, f"confidence must be a number in [0, 1], got {c!r}")
    r = obj["reasoning"]
    if not isinstance(r, str):
        return ParseResult("invalid_values", None, "reasoning must be a string")

    return ParseResult("ok", {"verdict": v.strip().upper(), "confidence": float(c), "reasoning": r}, "ok")


def _extract_object(raw: str) -> dict[str, Any] | None:
    """Return the one JSON object in the text, trying the whole text first.

    Two or more top-level objects is treated as no answer: a reply carrying both a PASS and a FAIL
    object (for example after an injection got through to the judge) must not be resolved by position.
    """
    text = raw.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        i = end
    return found[0] if len(found) == 1 else None

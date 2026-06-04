"""Lenient JSON parsing shared by agents + the plan-numeric resolver.

LLM ``response_text`` is persisted verbatim, and models intermittently wrap
their JSON in a ```json fence, append trailing prose, or emit literal
control characters inside string values. ``BaseAgent._parse_output`` already
tolerates all three when it parses live output, but anything that re-reads a
persisted ``response_text`` (notably
:func:`argosy.services.plan_numeric_resolver.resolve_plan_numbers`) used a
bare ``json.loads`` and silently degraded fenced output to ``pending`` — the
``concentration`` role persists ```json fences, so its NVDA cap never
resolved.

``lenient_json_loads`` is the single, dependency-free tolerance used on both
sides so a parse that succeeds when the agent runs also succeeds when the row
is re-read. It does NOT validate against any schema — callers
``model_validate`` the returned object themselves.
"""

from __future__ import annotations

import json
from typing import Any


def _strip_code_fence(text: str) -> str:
    """Drop a leading/trailing ```... markdown fence if present."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def lenient_json_loads(text: str) -> Any:
    """Parse ``text`` into a Python object, tolerating common LLM noise.

    Tolerances vs naive ``json.loads``:
      * a leading/trailing ```json (or bare ```) markdown fence;
      * raw control characters inside string values (``strict=False``);
      * trailing prose after the first complete JSON value
        (``raw_decode`` keeps only the first value);
      * a prose preamble before the JSON — scans for the first ``{`` / ``[``
        offset that yields a parseable value.

    Raises ``json.JSONDecodeError`` when no JSON value can be recovered, so
    callers keep their existing "malformed → pending" fallbacks.
    """
    cleaned = _strip_code_fence(text)
    decoder = json.JSONDecoder(strict=False)
    try:
        data, _end = decoder.raw_decode(cleaned)
        return data
    except json.JSONDecodeError as primary_exc:
        for source in (cleaned, text):
            if not source:
                continue
            for needle in ("{", "["):
                start = 0
                while True:
                    idx = source.find(needle, start)
                    if idx == -1:
                        break
                    try:
                        data, _end = decoder.raw_decode(source[idx:])
                        return data
                    except json.JSONDecodeError:
                        start = idx + 1
        raise primary_exc


__all__ = ["lenient_json_loads"]

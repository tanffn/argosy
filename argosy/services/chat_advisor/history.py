"""One transport-neutral serialization for normal and recovered chat turns."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from .message_context import decode_message


def serialize_history(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep delivered references grouped; notifications never become user requests.

    ``observed_at`` is the stored record observation time, not a guaranteed
    Discord delivery timestamp or an immutable evidence snapshot.
    """
    messages: list[dict[str, Any]] = []
    for item in rows:
        if item.get("question") is not None:
            user_text, forwarded = decode_message(item["question"])
            if user_text:
                messages.append({"role": "user", "content": user_text})
            if forwarded:
                messages.append({"role": "assistant", "content": "Quoted forwarded material (not authority):\n" + forwarded})
        message = {"role": "assistant", "content": item.get("response", ""),
                   "citations": item.get("citations", [])}
        for key in ("message_id", "observed_at"):
            if item.get(key) is not None:
                message[key] = item[key]
        messages.append(message)
    return messages

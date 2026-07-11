"""Minimal shared ticker-alias rules."""
from __future__ import annotations

import re

_CLASS_SYMBOL = re.compile(r"^([A-Z][A-Z0-9]*)([./-])([A-Z])$")


def equivalent_class_symbols(symbol: str) -> tuple[str, ...]:
    """Return equivalent one-letter share-class delimiter variants."""
    normalized = str(symbol or "").strip().upper()
    match = _CLASS_SYMBOL.fullmatch(normalized)
    if match is None:
        return (normalized,) if normalized else ()
    root, _, share_class = match.groups()
    return tuple(f"{root}{separator}{share_class}" for separator in "-./")

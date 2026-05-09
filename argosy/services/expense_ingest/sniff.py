"""Format detection. Content sniff is canonical; filename is a hint only."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from argosy.services.expense_ingest.types import ParserName


class UnknownFormatError(Exception):
    """Raised when a file matches no known issuer's signature."""

    def __init__(self, msg: str, sheets: list[str] | None = None,
                 head: bytes | None = None):
        super().__init__(msg)
        self.sheets = sheets
        self.head = head


def detect_format(path: Path) -> ParserName:
    """Return the parser to use for this file.

    Sniff order:
      1. Read first 512 bytes.
      2. If starts with '<HTML' / '<html' → assume Leumi HTML-as-xls.
      3. If starts with PK zip header → it's an .xlsx; look at sheet names.
         - 'פירוט עסקאות' → Isracard
         - sheet starting with 'לאומי לישראל' → Max
         - 'עסקאות במועד החיוב' (or similar) → Discount (TBD when sample arrives)
         - other recognized sheets → Cal/Amex/Diners (stubs for now)
      4. Otherwise raise UnknownFormatError.
    """
    with open(path, "rb") as f:
        head = f.read(512)

    stripped = head.lstrip()
    if stripped.startswith(b"<HTML") or stripped.startswith(b"<html"):
        return ParserName.LEUMI_OSH

    if head[:4] == b"PK\x03\x04":          # ZIP magic = .xlsx
        try:
            xl = pd.ExcelFile(path)
        except Exception as e:
            raise UnknownFormatError(f"could not open xlsx: {e}", head=head[:64])
        sheets = xl.sheet_names
        if "פירוט עסקאות" in sheets:
            return ParserName.ISRACARD
        if any(s.startswith("לאומי לישראל") for s in sheets):
            return ParserName.MAX
        # Discount Bank Mastercard: sheet 'עסקאות במועד החיוב' + 'עסקאות חו"ל ומט"ח'
        if "עסקאות במועד החיוב" in sheets:
            return ParserName.DISCOUNT
        raise UnknownFormatError(
            f"xlsx with no recognized sheet: {sheets}", sheets=sheets,
        )

    raise UnknownFormatError(
        f"unrecognized file header: {head[:64]!r}", head=head[:64],
    )

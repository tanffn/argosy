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
      1. Read first 512 bytes (header magic).
      2. If starts with '<HTML' / '<html' → Leumi HTML-as-xls.
         Classify the account view via the shared header-anchored helper
         (cash NIS / cash USD / custody). Custody and ambiguous FX
         headers raise — never route a custody export to a cash parser.
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
        from argosy.services.expense_ingest.parsers.leumi_html import (
            LeumiAccountView,
            LeumiAmbiguousHeaderError,
            LeumiCustodyViewError,
            classify_leumi_account_view,
            read_leumi_html,
        )
        try:
            text = read_leumi_html(path)
            view = classify_leumi_account_view(text)
        except LeumiAmbiguousHeaderError as e:
            raise UnknownFormatError(str(e), head=head[:64]) from e
        except OSError as e:
            raise UnknownFormatError(
                f"could not read Leumi HTML: {e}", head=head[:64],
            ) from e
        if view is LeumiAccountView.CUSTODY:
            raise LeumiCustodyViewError(
                f"{path.name} is a Leumi securities-custody export "
                "(ני\"ע נסחרים בחו\"ל), not a cash ledger — rejected at "
                "sniff so it cannot reach leumi_osh / leumi_usd."
            )
        if view is LeumiAccountView.CASH_USD:
            return ParserName.LEUMI_USD
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
        # Cal rolling last-90-days export (card 6225; parsed by the max-format module).
        if "פירוט עסקאות וזיכויים" in sheets:
            return ParserName.MAX
        # 'עסקאות במועד החיוב' format (parser family 'discount'; observed on the
        # MAX-branded card 2923 — owner-corrected 2026-07-12. Format name is
        # historical; sources carry the true brand).
        if "עסקאות במועד החיוב" in sheets:
            return ParserName.DISCOUNT
        raise UnknownFormatError(
            f"xlsx with no recognized sheet: {sheets}", sheets=sheets,
        )

    raise UnknownFormatError(
        f"unrecognized file header: {head[:64]!r}", head=head[:64],
    )

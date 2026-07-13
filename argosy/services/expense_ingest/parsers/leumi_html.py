"""Shared Leumi HTML-as-xls helpers: one read, header-anchored view class.

The FX cash ledger (פמ"ח) and the foreign-securities CUSTODY sub-account
share the same report chrome ('תנועות בחשבון מט"ח') and top account
number. Discriminating them by a fixed prefix length is unsafe — the
header offset drifted ~1.6k chars Jun→Jul 2026 and a custody marker can
silently slip past a 20k cut (false-ACCEPT → $215k-class double-count).

Contract:
  * Read the file ONCE; callers reuse the returned text.
  * Anchor on the account-descriptor region (first ``חשבון`` onward),
    not a whole-file prefix scan.
  * Positively identify cash (``פמ"ח`` / visual ``ח"מפ``) vs custody
    (``נסחרים`` / visual ``םירחסנ``). Raise when neither is found on an
    FX-shaped export. NIS cash (Osh) has neither FX token — accepted as
    Osh only when custody markers are absent.
"""

from __future__ import annotations

import enum
import re
from pathlib import Path

# Unicode formatting marks Leumi interleaves between Hebrew labels and
# Latin digits — strip before any marker / account regex.
_LEUMI_BIDI_MARKS_RE = re.compile(r"[‎‏‪-‮]")

# Account-descriptor tokens (logical + common visual/bidi renderings).
_CASH_FX_MARKERS = ('פמ"ח', 'ח"מפ')
_CUSTODY_MARKERS = ("נסחרים", "םירחסנ")

# Window after the ``חשבון`` anchor that holds the sub-account name +
# currency. Live Jun/Jul USD samples put the name ~4k chars after the
# anchor; keep headroom without scanning the whole CSS-bloated file.
_HEADER_WINDOW = 8_000


class LeumiAccountView(enum.Enum):
    CASH_NIS = "cash_nis"       # Osh current-account ledger
    CASH_USD = "cash_usd"       # פמ"ח FX cash ledger
    CUSTODY = "custody"         # ני"ע נסחרים בחו"ל securities view


class LeumiCustodyViewError(ValueError):
    """Securities-custody sub-account view — not a cash ledger.

    Ingesting it double-counts trades already booked in the cash
    statement (live incident 2026-07-13: 20 phantom rows, $215k phantom
    credits).
    """


class LeumiAmbiguousHeaderError(ValueError):
    """FX-shaped Leumi HTML whose header has neither cash nor custody
    descriptor — fail closed rather than guess."""


def read_leumi_html(path: Path) -> str:
    """Single UTF-8 read with bidi marks stripped. Callers MUST reuse."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    return _LEUMI_BIDI_MARKS_RE.sub("", text)


def account_header_region(text: str) -> str:
    """Slice from the first ``חשבון`` label through ``_HEADER_WINDOW``.

    Falls back to a leading slice only for tiny fixtures that lack the
    label (unit-test stubs) — production exports always carry it.
    """
    idx = text.find("חשבון")
    if idx < 0:
        return text[:_HEADER_WINDOW]
    return text[idx : idx + _HEADER_WINDOW]


def classify_leumi_account_view(text: str) -> LeumiAccountView:
    """Return the account view for a Leumi HTML body (already bidi-stripped).

    Raises:
      LeumiAmbiguousHeaderError: FX-shaped export with neither cash nor
        custody descriptor in the anchored header region.
    """
    region = account_header_region(text)
    has_cash_fx = any(m in region for m in _CASH_FX_MARKERS)
    has_custody = any(m in region for m in _CUSTODY_MARKERS)
    has_dollar = "דולר" in region
    has_fx_chrome = has_dollar or 'מט"ח' in region

    # Positive cash ID wins. A cash export's header chrome can also
    # mention the custody sub-account name in a selector list; treating
    # "both present in the 8k window" as custody false-rejects Jul cash
    # (codex-tandem 2026-07-13). Custody only when cash markers are absent.
    if has_cash_fx:
        return LeumiAccountView.CASH_USD
    if has_custody:
        return LeumiAccountView.CUSTODY
    if has_fx_chrome:
        # FX chrome without a positive cash/custody name → refuse.
        raise LeumiAmbiguousHeaderError(
            "Leumi FX export header has neither פמ\"ח cash nor "
            "נסחרים custody descriptor near the account label — "
            "refusing to classify (fail-closed)."
        )
    # NIS Osh (or non-FX HTML): no FX chrome, no custody name.
    return LeumiAccountView.CASH_NIS


def is_custody_view_text(text: str) -> bool:
    try:
        return classify_leumi_account_view(text) is LeumiAccountView.CUSTODY
    except LeumiAmbiguousHeaderError:
        return False


def is_custody_view(path: Path) -> bool:
    """Path-level custody check (one read). Prefer text-form when the
    caller already loaded the file."""
    try:
        return is_custody_view_text(read_leumi_html(path))
    except OSError:
        return False


def raise_if_custody(path: Path, *, text: str | None = None) -> str:
    """Load (or reuse) HTML; raise ``LeumiCustodyViewError`` on custody.

    Returns the stripped text so the caller can continue without a
    second read. Also raises ``LeumiAmbiguousHeaderError`` for FX HTML
    that cannot be classified.
    """
    body = text if text is not None else read_leumi_html(path)
    view = classify_leumi_account_view(body)
    if view is LeumiAccountView.CUSTODY:
        raise LeumiCustodyViewError(
            f"{path.name} is the foreign-securities custody view "
            "(ני\"ע נסחרים בחו\"ל) of the Leumi account, not a cash "
            "ledger — its rows are value-date clearing pairs that would "
            "double-count trades already booked in the cash statement. "
            "Export the פמ\"ח (עו\"ש מט\"ח) or Osh cash sub-account instead."
        )
    return body


__all__ = [
    "LeumiAccountView",
    "LeumiAmbiguousHeaderError",
    "LeumiCustodyViewError",
    "account_header_region",
    "classify_leumi_account_view",
    "is_custody_view",
    "is_custody_view_text",
    "raise_if_custody",
    "read_leumi_html",
    "_LEUMI_BIDI_MARKS_RE",
    "_CASH_FX_MARKERS",
    "_CUSTODY_MARKERS",
]

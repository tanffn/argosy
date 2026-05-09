"""Merchant-name normalization. Idempotent. The output is the cache key."""

from __future__ import annotations

import re
import unicodedata

# Hebrew installment markers. Both phrasings show up across issuers.
_INSTALLMENT_HE = re.compile(r"\bתשלום\s+\d+\s*(?:/|מתוך|מ-)\s*\d+\b")
_INSTALLMENT_MORE = re.compile(r"\bשלם\s+\d+\s+מתוך\s+\d+\b")

# Cards sometimes append a 4+-digit transaction sequence to the merchant string.
_TRAILING_DIGITS = re.compile(r"\s+\d{4,}\s*$")

# Trailing transaction-id appended with an asterisk (e.g. "NAME-CHEAP.COM*ABCD").
_TRAILING_STAR = re.compile(r"\s*\*[A-Z0-9_]+\s*$", re.IGNORECASE)

# Leumi current-account abbreviates merchants and tags them with '-י'
_LEUMI_SUFFIX = re.compile(r"-י\s*$")

# Foreign-merchant prefixes used by acquirers (PayPal, Square, etc.).
# For WWW we only strip the literal "WWW." prefix (dot consumed here), so that
# what follows (e.g. "NAME-CHEAP.COM") is preserved; the trailing *ALNUM is
# then removed by _TRAILING_STAR above.
_FOREIGN_PREFIX = re.compile(
    r"^(?:PAYPAL|SQ|SP|TST|WWW)\.?\s*\*?\s*",
    re.IGNORECASE,
)


def normalize(s: str) -> str:
    """Normalize a merchant string into a stable cache key.

    Lowercases (Latin only — Hebrew has no case), strips installment markers,
    trailing transaction-sequence digit blocks (>=4 digits only),
    trailing ``*ALPHANUM`` transaction-id blocks, Leumi's '-י' suffix,
    foreign-acquirer prefixes, and excess whitespace.

    Idempotent: ``normalize(normalize(s)) == normalize(s)``.
    """
    if s is None:
        return ""
    s = s.strip()
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = _INSTALLMENT_HE.sub("", s)
    s = _INSTALLMENT_MORE.sub("", s)
    s = _FOREIGN_PREFIX.sub("", s)
    s = _LEUMI_SUFFIX.sub("", s)
    s = _TRAILING_STAR.sub("", s)
    s = _TRAILING_DIGITS.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

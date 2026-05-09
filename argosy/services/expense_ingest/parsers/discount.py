"""Discount Bank Mastercard parser — TODO.

User has card 2923 with this format. The export has two sheets:
- 'עסקאות במועד החיוב' (transactions on charge date)
- 'עסקאות חו"ל ומט"ח' (foreign-currency transactions)

Header row at row 4 has 16 columns including a pre-categorized 'קטגוריה'
column and an FX rate column. When implementing, mirror the Max parser's
issuer_category preservation pattern; this issuer also pre-categorizes.
"""

from pathlib import Path

from argosy.services.expense_ingest.types import ParseResult


def parse(path: Path) -> ParseResult:
    raise NotImplementedError(
        "Discount parser not yet implemented. The format has 16 columns "
        "across two sheets ('עסקאות במועד החיוב', 'עסקאות חו\"ל ומט\"ח'); "
        "implement when prioritized in a follow-up."
    )

"""Amex credit-card parser — TODO when sample arrives."""

from pathlib import Path

from argosy.services.expense_ingest.types import ParseResult


def parse(path: Path) -> ParseResult:
    raise NotImplementedError(
        "Amex parser not yet implemented. Provide a sample file and "
        "extend tests/fixtures/expenses/_make_amex_fixture.py."
    )

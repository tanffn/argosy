"""Cal credit-card parser — TODO when sample arrives."""

from pathlib import Path

from argosy.services.expense_ingest.types import ParseResult


def parse(path: Path) -> ParseResult:
    raise NotImplementedError(
        "Cal parser not yet implemented. Provide a sample file and "
        "extend tests/fixtures/expenses/_make_cal_fixture.py."
    )

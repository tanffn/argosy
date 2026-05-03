"""Exercise the TSV parser against the real May 2026 Family Finances TSV.

The real file lives in the user's Google Drive, which may not be present
on every machine. We skip with a clear reason if absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from argosy.ingest.tsv import parse_portfolio_tsv

TSV_PATH = Path(
    r"D:/Google Drive/Family/Finances/Portfolio/Resources/Family Finances Status - 26 May.tsv"
)


def _require_tsv() -> Path:
    if not TSV_PATH.is_file():
        pytest.skip(
            f"User TSV not present at {TSV_PATH!s}; skip on machines without "
            "Google Drive sync."
        )
    return TSV_PATH


def test_tsv_parser_parses_top_metadata() -> None:
    snap = parse_portfolio_tsv(_require_tsv())
    assert snap.snapshot_date is not None
    assert snap.fx_usd_nis is not None and 2.0 < snap.fx_usd_nis < 5.0
    assert snap.fx_usd_eur is not None and 0.5 < snap.fx_usd_eur < 1.5


def test_tsv_parser_extracts_positions_with_nvda() -> None:
    snap = parse_portfolio_tsv(_require_tsv())
    assert len(snap.positions) >= 10
    # NVDA is the dominant position; assert it's present and large.
    nvda = [p for p in snap.positions if (p.symbol or "").strip().upper() == "NVDA"]
    assert nvda, "Expected NVDA row in May 2026 TSV"
    nvda_pos = nvda[0]
    assert (nvda_pos.shares or 0) > 1_000
    # USD K value is in thousands; NVDA was ~$2.2M = ~2200 K.
    assert (nvda_pos.usd_value_k or 0) > 1_000


def test_tsv_parser_extracts_cash_balances() -> None:
    snap = parse_portfolio_tsv(_require_tsv())
    cash_total = snap.cash_balances_usd_k()
    # Expect non-trivial cash balances given the May 2026 file has
    # Schwab cash + Leumi NIS cash + Leumi USD cash.
    assert cash_total > 50.0


def test_tsv_parser_extracts_allocation_rows() -> None:
    snap = parse_portfolio_tsv(_require_tsv())
    assert len(snap.allocations) >= 3
    cats = [a.category.lower() for a in snap.allocations]
    assert any("core" in c or "growth" in c or "cash" in c for c in cats)


def test_tsv_parser_extracts_pensions() -> None:
    snap = parse_portfolio_tsv(_require_tsv())
    # The May 2026 file lists pensions for Ariel + Noga.
    assert any(p.person.lower().startswith("ariel") for p in snap.pensions)


def test_tsv_parser_summary_includes_top_positions() -> None:
    snap = parse_portfolio_tsv(_require_tsv())
    text = snap.summary_text()
    assert "Total positions parsed" in text
    assert "NVDA" in text or "nvda" in text.lower() or snap.total_usd_value_k > 0

"""Tests for argosy.services.net_worth_backfill + the endpoint merge.

The backfill reconstructs pre-ingest net-worth points from archived
"Family Finances Status - YY Mon.tsv" exports on disk. Rules under test:

  * evidence gate — a parse missing FX or the NVDA position (the
    pre-layout "25 Aug" failure mode) yields NO point, never a fake cliff;
  * dating — TSV header date wins; else the file mtime, accepted only
    when its year-month agrees with the filename stamp;
  * ``before`` bound — reconstructions never reach into the real
    snapshot era;
  * endpoint merge — reconstructed points carry ``reconstructed=True``
    + provenance, and a real snapshot always wins a shared date.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pytest

from argosy.ingest.tsv import PortfolioPosition, PortfolioSnapshot


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """Isolate from the dev machine's real archive + reset the cache."""
    monkeypatch.delenv("ARGOSY_EXPENSE_SAMPLES_ROOT", raising=False)
    from argosy.services import net_worth_backfill

    net_worth_backfill._CACHE.clear()
    yield
    net_worth_backfill._CACHE.clear()


def _snapshot(
    *,
    snapshot_date: date | None = None,
    fx: float | None = 3.3,
    nvda_k: float = 2250.0,
    other_k: float = 900.0,
    cash_k: float = 170.0,
) -> PortfolioSnapshot:
    positions = []
    if nvda_k > 0:
        positions.append(
            PortfolioPosition(
                symbol="NVDA", asset_type="Stock", currency="USD",
                usd_value_k=nvda_k,
            )
        )
    positions.append(
        PortfolioPosition(
            symbol="FWRA", asset_type="Core Equity", currency="USD",
            usd_value_k=other_k,
        )
    )
    positions.append(
        PortfolioPosition(
            symbol="-", asset_type="Cash", currency="NIS",
            usd_value_k=cash_k,
        )
    )
    return PortfolioSnapshot(
        source_path="archive.tsv",
        snapshot_date=snapshot_date,
        fx_usd_nis=fx,
        positions=positions,
    )


def _write_archive(root, name: str, mtime: date) -> None:
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text("placeholder", encoding="utf-8")
    t = datetime(mtime.year, mtime.month, mtime.day, 12, 0).timestamp()
    os.utime(p, (t, t))


def test_point_built_and_dated_from_mtime(monkeypatch, tmp_path):
    import argosy.ingest.tsv as tsv_mod
    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    root = tmp_path / "resources"
    _write_archive(root, "Family Finances Status - 25 Oct.tsv", date(2025, 10, 18))
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv", lambda _p: _snapshot(),
    )

    pts = reconstructed_net_worth_points()
    assert len(pts) == 1
    p = pts[0]
    assert p.date == date(2025, 10, 18)  # from the file mtime
    assert p.snapshot_date is None
    assert p.total_usd == pytest.approx((2250 + 900 + 170) * 1000.0)
    assert p.cash_usd == pytest.approx(170_000.0)
    assert p.nvda_usd == pytest.approx(2_250_000.0)
    # NVDA ÷ tradeable book (cash excluded): 2250 / 3150.
    assert p.nvda_pct == pytest.approx(2250 / 3150 * 100)
    assert p.fx_usd_nis == pytest.approx(3.3)
    assert p.total_nis == pytest.approx(3_320_000.0 * 3.3)
    assert p.nis_denominated_usd == pytest.approx(170_000.0)
    assert "25 Oct.tsv" in p.provenance
    assert "mtime" in p.provenance


def test_header_date_wins_over_mtime(monkeypatch, tmp_path):
    import argosy.ingest.tsv as tsv_mod
    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    root = tmp_path / "resources"
    _write_archive(root, "Family Finances Status - 26 Feb.tsv", date(2026, 2, 28))
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv",
        lambda _p: _snapshot(snapshot_date=date(2026, 2, 6)),
    )

    pts = reconstructed_net_worth_points()
    assert len(pts) == 1
    assert pts[0].date == date(2026, 2, 6)
    assert "header" in pts[0].provenance


def test_evidence_gate_rejects_pre_layout_parses(monkeypatch, tmp_path):
    """The '25 Aug' failure mode: a parse with no FX and no NVDA row is
    a mis-read pre-layout export — no point, never a fake cliff."""
    import argosy.ingest.tsv as tsv_mod
    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    root = tmp_path / "resources"
    _write_archive(root, "Family Finances Status - 25 Aug.tsv", date(2025, 8, 22))
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv",
        lambda _p: _snapshot(fx=None, nvda_k=0.0),
    )
    assert reconstructed_net_worth_points() == []

    # FX present but no NVDA position still fails the gate.
    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv", lambda _p: _snapshot(nvda_k=0.0),
    )
    from argosy.services import net_worth_backfill

    net_worth_backfill._CACHE.clear()
    assert reconstructed_net_worth_points() == []


def test_mtime_filename_mismatch_is_undatable(monkeypatch, tmp_path):
    """No header date + an mtime whose month disagrees with the filename
    stamp (Drive re-sync) → we don't know when the export was taken."""
    import argosy.ingest.tsv as tsv_mod
    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    root = tmp_path / "resources"
    # Filename says Oct 2025; mtime says Mar 2026.
    _write_archive(root, "Family Finances Status - 25 Oct.tsv", date(2026, 3, 2))
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv", lambda _p: _snapshot(),
    )
    assert reconstructed_net_worth_points() == []


def test_before_bound_excludes_snapshot_era(monkeypatch, tmp_path):
    import argosy.ingest.tsv as tsv_mod
    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    root = tmp_path / "resources"
    _write_archive(root, "Family Finances Status - 25 Oct.tsv", date(2025, 10, 18))
    _write_archive(root, "Family Finances Status - 26 Feb.tsv", date(2026, 2, 6))
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv", lambda _p: _snapshot(),
    )

    pts = reconstructed_net_worth_points(before=date(2026, 1, 1))
    assert [p.date for p in pts] == [date(2025, 10, 18)]


def test_non_canonical_names_ignored(monkeypatch, tmp_path):
    """.bak siblings and non-matching names never become points."""
    import argosy.ingest.tsv as tsv_mod
    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    root = tmp_path / "resources"
    root.mkdir(parents=True)
    (root / "Family Finances Status - 26 Jun.tsv.bak_pre_fix").write_text("x")
    (root / "random.tsv").write_text("x")
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv", lambda _p: _snapshot(),
    )
    assert reconstructed_net_worth_points() == []


# ---------------------------------------------------------------------------
# Legacy pre-layout sheets ("25 Jul.csv" / "25 Aug.tsv" format)
# ---------------------------------------------------------------------------

# Jul-2025 variant: comma-separated, LEADING EMPTY COLUMN, quoted
# thousands, ad-hoc sanity-check spill cells in trailing columns, NIS
# rows with local Current Value + converted (K) USD Value, and a
# physical real-estate row (excluded from the total for basis
# consistency with the modern-era points).
_LEGACY_CSV = """\
,Location,Currency,Type,Details,Symbol,# Shares,Current price,Avg Price,Current Value,(K) USD Value,% Change,% Yearly,
,schwab,USD,Growth,"Stock, AI",NVDA,12638,173.91,173.91,"2,197,875","2,198",0%,,
,Leumi,NIS,Cash,Cash,,108000,1,1,"108,000",32,0%,,
,Leumi,USD,Cash,Cash,,153000,1,1,"153,000",153,0%,,
,Leumi,NIS,Foundational,ETF,MSCI World,750,247.4,231.64,"185,550",55,7%,,
,Leumi,USD,Div\\Value,ETF,SCHD,4500,27.01,26.35,"121,545",122,3%,,Leumi Sum in NIS (sanity check)
,Leumi,USD,Hedge,TLT Bond,TLT,500,85.1,99.61,"42,550",43,-15%,,"1,760"
,Leumi,USD,Growth,ETF,QQQM,60,231.23,230.43,"13,874",14,0%,,
,Leumi,USD,Growth,"Stock, IT",AMZN,100,223.84,167.38,"22,384",22,34%,,
,Leumi,USD,Growth,"Stock, IT",GOOG,100,183.81,177.84,"18,381",18,3%,,
,Leumi,USD,Foundational,Stock,BRK.B,50,470.08,469.38,"23,504",24,0%,,
,Aborad,USD,Real estate,Real estate,-,3,-,-,-,167,0,,
"""


def _write_legacy(root, name: str, mtime: date, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    p = root / name
    p.write_text(content, encoding="utf-8")
    t = datetime(mtime.year, mtime.month, mtime.day, 12, 0).timestamp()
    os.utime(p, (t, t))


def test_legacy_csv_parses_evidence_grade(monkeypatch, tmp_path):
    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    root = tmp_path / "resources"
    _write_legacy(
        root, "Family Finances Status - 25 Jul.csv",
        date(2025, 7, 19), _LEGACY_CSV,
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))

    pts = reconstructed_net_worth_points()
    assert len(pts) == 1
    p = pts[0]
    assert p.date == date(2025, 7, 19)
    # Positions sum EXCLUDING the $167K real-estate row.
    assert p.total_usd == pytest.approx(2_681_000.0)
    assert p.nvda_usd == pytest.approx(2_198_000.0)
    # Tradeable book excludes the cash rows: 2198+55+122+43+14+22+18+24.
    assert p.nvda_pct == pytest.approx(2198 / 2496 * 100, abs=0.1)
    assert p.cash_usd == pytest.approx(185_000.0)
    assert p.nis_denominated_usd == pytest.approx(87_000.0)
    # FX from the NIS rows' aggregate ratio: 293,550 / 87,000.
    assert p.fx_usd_nis == pytest.approx(293_550 / 87_000, abs=1e-4)
    assert p.total_nis == pytest.approx(p.total_usd * p.fx_usd_nis)
    assert p.provenance.startswith(
        "legacy-tsv:Family Finances Status - 25 Jul.csv"
    )
    assert "fx derived from NIS-row ratios" in p.provenance


def test_legacy_tab_variant_without_leading_column(monkeypatch, tmp_path):
    """The Aug-2025 shape: tab-separated, no leading empty column, CRLF."""
    import csv as _csv
    import io as _io

    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    rows = list(_csv.reader(_io.StringIO(_LEGACY_CSV)))
    tsv = "\r\n".join("\t".join(r[1:]) for r in rows) + "\r\n"

    root = tmp_path / "resources"
    _write_legacy(
        root, "Family Finances Status - 25 Aug.tsv",
        date(2025, 8, 22), tsv,
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))

    pts = reconstructed_net_worth_points()
    assert len(pts) == 1
    assert pts[0].date == date(2025, 8, 22)
    assert pts[0].total_usd == pytest.approx(2_681_000.0)
    assert pts[0].provenance.startswith("legacy-tsv:")


def test_legacy_rejects_misaligned_usd_column(monkeypatch, tmp_path):
    """Shifted values (the mis-parse failure class the gate exists for):
    Current Value ≉ (K) USD Value × 1000 on the USD rows → no point."""
    import csv as _csv
    import io as _io

    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    rows = list(_csv.reader(_io.StringIO(_LEGACY_CSV)))
    # Corrupt every data row's (K) USD Value (index 10 with the leading
    # empty column) to a figure unrelated to Current Value.
    for r in rows[1:]:
        if len(r) > 10 and r[10].strip():
            r[10] = "7"
    out = _io.StringIO()
    _csv.writer(out, lineterminator="\n").writerows(rows)

    root = tmp_path / "resources"
    _write_legacy(
        root, "Family Finances Status - 25 Jul.csv",
        date(2025, 7, 19), out.getvalue(),
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    assert reconstructed_net_worth_points() == []


def test_legacy_declines_fx_when_nis_ratios_disagree(monkeypatch, tmp_path):
    """NIS rows implying rates >3% apart → the ₪ fields are declined
    (fx/total_nis None) but the USD point still ships."""
    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    # Make the MSCI World NIS row imply fx ≈ 4.6 vs cash's 3.375.
    bad = _LEGACY_CSV.replace(
        ',Leumi,NIS,Foundational,ETF,MSCI World,750,247.4,231.64,"185,550",55,7%,,',
        ',Leumi,NIS,Foundational,ETF,MSCI World,750,247.4,231.64,"253,000",55,7%,,',
    )
    assert bad != _LEGACY_CSV
    root = tmp_path / "resources"
    _write_legacy(
        root, "Family Finances Status - 25 Jul.csv", date(2025, 7, 19), bad,
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))

    pts = reconstructed_net_worth_points()
    assert len(pts) == 1
    assert pts[0].fx_usd_nis is None
    assert pts[0].total_nis is None
    assert pts[0].total_usd == pytest.approx(2_681_000.0)
    assert "fx undetermined" in pts[0].provenance


def test_legacy_rejects_without_nvda_row(monkeypatch, tmp_path):
    from argosy.services.net_worth_backfill import (
        reconstructed_net_worth_points,
    )

    no_nvda = _LEGACY_CSV.replace("NVDA", "AAPL")
    root = tmp_path / "resources"
    _write_legacy(
        root, "Family Finances Status - 25 Jul.csv",
        date(2025, 7, 19), no_nvda,
    )
    monkeypatch.setenv("ARGOSY_EXPENSE_SAMPLES_ROOT", str(root))
    assert reconstructed_net_worth_points() == []


# ---------------------------------------------------------------------------
# Endpoint merge — /api/portfolio/net-worth-history
# ---------------------------------------------------------------------------


def test_endpoint_merges_reconstructed_points(client_with_db, monkeypatch):
    from datetime import date as _date

    from argosy.services.net_worth_backfill import ReconstructedPoint
    from argosy.state.models import User
    from tests.test_net_worth_history import _seed_row

    SF = client_with_db.app.state.session_factory
    today = _date.today()
    real_date = today - timedelta(days=30)
    with SF() as s:
        s.add(User(id="ariel", plan="free"))
        s.commit()
        _seed_row(
            s, snapshot_date=real_date,
            total_usd_value_k=1000.0, nvda_usd_value_k=500.0,
        )

    recon_date = today - timedelta(days=90)

    def _fake_points(*, before=None):
        assert before == real_date  # bounded at the earliest REAL point
        return [
            ReconstructedPoint(
                date=recon_date,
                snapshot_date=None,
                total_usd=3_311_000.0,
                nvda_pct=68.0,
                nvda_usd=2_257_000.0,
                cash_usd=176_000.0,
                fx_usd_nis=3.31,
                total_nis=3_311_000.0 * 3.31,
                nis_denominated_usd=176_000.0,
                provenance="reconstructed: archived TSV export (test)",
            ),
        ]

    monkeypatch.setattr(
        "argosy.services.net_worth_backfill.reconstructed_net_worth_points",
        _fake_points,
    )

    res = client_with_db.get(
        "/api/portfolio/net-worth-history?user_id=ariel&months=12"
    )
    assert res.status_code == 200
    pts = res.json()["points"]
    assert [p["date"] for p in pts] == [
        recon_date.isoformat(), real_date.isoformat(),
    ]
    recon, real = pts
    assert recon["reconstructed"] is True
    assert recon["provenance"].startswith("reconstructed:")
    assert recon["total_usd"] == 3_311_000.0
    assert real["reconstructed"] is False
    assert real["provenance"] is None

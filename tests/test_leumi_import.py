"""Leumi balance extraction + position building."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from argosy.adapters.brokers.leumi_portfolio_xls import (
    LeumiHolding,
    LeumiPortfolio,
)
from argosy.services.leumi_import import (
    LeumiCashBalance,
    build_leumi_positions,
    read_balance,
)


@pytest.mark.parametrize("replace_usd", [False, True])
def test_holdings_only_import_preserves_omitted_cash_dates_and_other_accounts(tmp_path, monkeypatch, replace_usd):
    import json
    import sqlalchemy as sa
    from sqlalchemy.orm import Session
    from argosy.services.leumi_import import import_leumi
    from argosy.state.models import Base, PortfolioSnapshotRow, User

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'import.db'}")
    Base.metadata.create_all(engine)
    observed = date(2026, 8, 22)
    incoming = date(2026, 9, 11)
    positions = [
        {"location": "Leumi", "asset_type": "Cash", "currency": currency,
         "usd_value_k": value, "current_value_local": value * 1000,
         "observed_as_of": observed.isoformat(), "valued_as_of": observed.isoformat()}
        for currency, value in (("NIS", 15), ("USD", 112), ("EUR", 7))
    ] + [{"location": "IBKR", "asset_type": "Stock", "currency": "USD", "symbol": "AAPL", "shares": 10, "current_price": 200, "usd_value_k": 2}]
    portfolio = LeumiPortfolio(incoming, "test", 1, 10000, (
        LeumiHolding("1", "EXUS", "EXUS", "LN", 100, 90, 100, 10000, 0, 1),
    ))
    monkeypatch.setattr("argosy.services.leumi_import.parse_portfolio_xls", lambda path: portfolio)
    monkeypatch.setattr("argosy.services.leumi_import.read_balance", lambda path: 0.0)
    monkeypatch.setattr("argosy.services.fx.rate", lambda *args: 3.0)
    with Session(engine) as session:
        session.add(User(id="import-test"))
        session.add(PortfolioSnapshotRow(user_id="import-test", snapshot_date=observed,
            imported_at=datetime(2026, 8, 22, tzinfo=UTC),
            source_path="prior", positions_json=json.dumps(positions),
            totals_json='{"total_usd_value_k":136}', fx_usd_nis=3.0))
        session.commit()
        report = import_leumi(session, user_id="import-test", portfolio_path=tmp_path / "new.xls",
            fx_paths={"USD": tmp_path / "usd.xls"} if replace_usd else None, apply=True)
        stored = session.get(PortfolioSnapshotRow, report.snapshot_id)
        rows = json.loads(stored.positions_json)
        cash = {p["currency"]: p for p in rows if p["asset_type"] == "Cash"}
        assert set(cash) == {"NIS", "USD", "EUR"}
        for currency in ("NIS", "EUR") + (() if replace_usd else ("USD",)):
            assert cash[currency]["observed_as_of"] == observed.isoformat()
            assert cash[currency]["valued_as_of"] == observed.isoformat()
        assert cash["USD"]["usd_value_k"] == (0 if replace_usd else 112)
        if replace_usd:
            assert cash["USD"]["valued_as_of"] == incoming.isoformat()
        assert len(report.cash_carried) == (2 if replace_usd else 3)
        assert len([p for p in rows if p["location"] == "IBKR"]) == 1
        assert next(p for p in rows if p.get("symbol") == "EXUS")["valued_as_of"] == incoming.isoformat()
        assert len(json.loads(stored.parse_warnings_json)) >= len(report.cash_carried)
        assert stored.source_path == str((tmp_path / "new.xls").resolve())
    engine.dispose()

DRIVE = Path("D:/Google Drive/Family/Finances/Portfolio/Resources/2026/Leumi")


@pytest.mark.parametrize("apply", [False, True])
def test_cli_catalogs_raw_exports_only_when_applying(tmp_path, monkeypatch, apply):
    from types import SimpleNamespace
    from argosy.cli.ingest import ingest_leumi_portfolio

    portfolio = tmp_path / "holdings.xls"
    portfolio.write_bytes(b"holdings")
    cash = tmp_path / "cash.xls"
    cash.write_bytes(b"balance")
    calls = []

    async def catalog(**kwargs):
        calls.append(("catalog", kwargs))

    def importer(*args, **kwargs):
        calls.append(("import", kwargs))
        return SimpleNamespace(lines=lambda: [])

    monkeypatch.setattr("argosy.services.file_catalog.catalog_upload", catalog)
    monkeypatch.setattr("argosy.services.leumi_import.import_leumi", importer)
    monkeypatch.setattr("argosy.config.get_settings", lambda: SimpleNamespace(database_url="sqlite://"))
    ingest_leumi_portfolio(portfolio=portfolio, ils=None, usd=cash, eur=None, user_id="test", apply=apply)
    assert [entry[0] for entry in calls] == (["catalog", "catalog", "import"] if apply else ["import"])
    if apply:
        assert calls[0][1]["raw_bytes"] == b"holdings"
        assert calls[1][1]["raw_bytes"] == b"balance"
        assert all(entry[1]["source"] == "intake_upload" for entry in calls[:-1])
ILS_FILE = DRIVE / "תנועות בחשבון 22_8_2026.xls"
USD_FILE = DRIVE / "תנועות בחשבון מטח 22-08-2026 (1).xls"
EUR_FILE = DRIVE / "תנועות בחשבון מטח 22-08-2026 (2).xls"


def _html(cells: list[str]) -> str:
    return "<HTML><table>" + "".join(f"<td>{c}</td>" for c in cells) + "</table></HTML>"


class TestReadBalance:
    def test_ils_layout_label_and_value_in_one_cell(self, tmp_path):
        f = tmp_path / "osh.xls"
        f.write_text(_html(["בנק לאומי", "היתרה\n   \n  ₪\n  47,100.50",
                            "מסגרת האשראי\n  ₪\n  0.00"]), encoding="utf-8")
        assert read_balance(f) == 47_100.50

    def test_fx_layout_value_in_the_next_cell(self, tmp_path):
        f = tmp_path / "fx.xls"
        f.write_text(_html(['יתרת עו"ש:', "112,960.99"]), encoding="utf-8")
        assert read_balance(f) == 112_960.99

    def test_does_not_read_past_the_movements_table(self, tmp_path):
        """The regression that motivated the stop marker: 'היתרה בש"ח' is a
        COLUMN HEADER inside the table, and reading on returned a
        transaction's reference number (13104) as the ILS balance."""
        f = tmp_path / "osh.xls"
        f.write_text(_html([
            "היתרה\n  ₪\n  47,100.50",
            "תנועות בחשבון",
            'היתרה בש"ח', "13104", "276.00",
        ]), encoding="utf-8")
        assert read_balance(f) == 47_100.50

    def test_raises_when_there_is_no_balance(self, tmp_path):
        f = tmp_path / "x.xls"
        f.write_text(_html(["תנועות בחשבון", 'היתרה בש"ח', "13104"]), encoding="utf-8")
        with pytest.raises(ValueError, match="no closing balance"):
            read_balance(f)

    @pytest.mark.skipif(not ILS_FILE.is_file(), reason="Drive exports absent")
    def test_against_the_real_exports(self):
        assert read_balance(ILS_FILE) == 47_100.50
        assert read_balance(USD_FILE) == 112_960.99
        assert read_balance(EUR_FILE) == 6_108.31


class TestBuildPositions:
    def _portfolio(self):
        return LeumiPortfolio(
            as_of=date(2026, 8, 22), account="882-447452/10",
            declared_count=2, declared_value_usd=300.0,
            holdings=(
                LeumiHolding("60183896", "(Ishares Core S&P 500) CSPX LN", "CSPX",
                             "LN", 240.0, 737.74, 827.54, 200.0, 10.0, 0.12),
                LeumiHolding("60321790", "(Sofi Technologies Inc) SOFI", "SOFI",
                             None, 2000.0, 12.09, 18.91, 100.0, 5.0, 0.06),
            ),
        )

    def test_every_row_is_stamped_leumi_and_dated_from_the_file(self):
        rows = build_leumi_positions(self._portfolio(), [], usd_ils=3.0, eur_usd=1.16)
        assert len(rows) == 2
        assert {r.location for r in rows} == {"Leumi"}
        assert {r.observed_as_of for r in rows} == {date(2026, 8, 22)}
        assert all(r.carried_forward is False for r in rows)

    def test_etfs_and_stocks_are_labelled(self):
        rows = build_leumi_positions(self._portfolio(), [], usd_ils=3.0, eur_usd=1.16)
        by = {r.symbol: r.asset_type for r in rows}
        assert by == {"CSPX": "ETF", "SOFI": "Stock"}

    def test_cash_is_converted_per_currency(self):
        bal = [
            LeumiCashBalance("NIS", 30_000.0, "ils.xls"),
            LeumiCashBalance("USD", 112_960.99, "usd.xls"),
            LeumiCashBalance("EUR", 6_108.31, "eur.xls"),
        ]
        rows = build_leumi_positions(self._portfolio(), bal, usd_ils=3.0, eur_usd=1.16)
        cash = {r.currency: r for r in rows if r.asset_type == "Cash"}
        assert cash["NIS"].usd_value_k == pytest.approx(10.0)
        assert cash["USD"].usd_value_k == pytest.approx(112.96099)
        assert cash["EUR"].usd_value_k == pytest.approx(7.0856396)
        # local amount is preserved untouched for every currency
        assert cash["NIS"].current_value_local == 30_000.0

    def test_rejects_a_nonsense_fx_rate(self):
        with pytest.raises(ValueError, match="usd_ils must be positive"):
            build_leumi_positions(self._portfolio(), [], usd_ils=0.0, eur_usd=1.16)


class TestRefusesAnUnreconciledFile:
    def test_import_aborts_when_the_file_disagrees_with_its_own_header(self, tmp_path):
        """A short read must never become a silent position wipe."""
        from argosy.services.leumi_import import import_leumi

        f = tmp_path / "p.xls"
        f.write_text(
            '<?xml version="1.0"?><Workbook><Table>'
            "<Row><Cell><Data>תאריך:</Data></Cell><Cell><Data>22.08.26</Data></Cell></Row>"
            "<Row><Cell><Data>מס' ניירות:</Data></Cell><Cell><Data>38</Data></Cell>"
            "<Cell><Data>שווי תיק עדכני ב$</Data></Cell><Cell><Data>999999.00</Data></Cell></Row>"
            "<Row><Cell><Data>מספר נייר</Data></Cell></Row>"
            "<Row><Cell><Data>60183896</Data></Cell><Cell><Data>(X) CSPX LN</Data></Cell>"
            "<Cell><Data></Data></Cell><Cell><Data>1</Data></Cell><Cell><Data>1</Data></Cell>"
            "<Cell><Data>1</Data></Cell><Cell><Data>100.00</Data></Cell>"
            "<Cell><Data>0</Data></Cell><Cell><Data>0</Data></Cell><Cell><Data>0</Data></Cell>"
            "<Cell><Data>1</Data></Cell></Row>"
            "</Table></Workbook>", encoding="utf-8")
        with pytest.raises(ValueError, match="refusing to import"):
            import_leumi(None, user_id="ariel", portfolio_path=f, apply=True)

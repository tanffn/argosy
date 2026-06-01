"""Tests for the plan_synthesis phase-1 input assembler."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from argosy.state.models import Base, User, PlanVersion


def _make_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    return Session()


def test_phase1_inputs_dataclass_has_all_required_fields():
    """Every kwarg required by a phase-1 analyst's build_prompt must be a
    field on Phase1Inputs."""
    from argosy.orchestrator.flows.plan_synthesis.inputs import Phase1Inputs

    required_fields = {
        "positions_summary", "plan_targets",
        "fx_payload",
        "tickers", "fundamentals_payload",
        "news_payload",
        "macro_snapshot",
        "social_payload",
        "lots_summary", "dividends_summary", "rsu_schedule_summary", "domain_kb_files",
        "indicators_payload",
        "plan_label", "plan_markdown", "snapshot_label", "snapshot_summary",
        "user_context_yaml", "recent_events",
    }
    actual_fields = set(Phase1Inputs.__dataclass_fields__.keys())
    missing = required_fields - actual_fields
    assert not missing, f"Phase1Inputs missing required fields: {missing}"


def test_assemble_returns_empty_defaults_when_session_is_empty(tmp_path, monkeypatch):
    """An empty-session + no-adapters world returns Phase1Inputs populated
    entirely with empty defaults. No raises."""
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        Phase1Inputs,
        assemble_phase1_inputs,
    )

    # Isolate ARGOSY_HOME so the TSV walker sees no TSVs and the adapters
    # have no config to find.
    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    try:
        from argosy.config import reload_settings

        reload_settings()
    except Exception:
        pass

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.commit()

    inputs = assemble_phase1_inputs(
        session,
        user_id="ariel",
        baseline=None,
        prior_current=None,
        decision_audit_token="plan-synth-42",
    )
    assert isinstance(inputs, Phase1Inputs)
    assert inputs.snapshot_label == "plan-synth-42"
    assert inputs.tickers == []
    assert inputs.fx_payload == {}
    assert inputs.fundamentals_payload == {}
    assert inputs.news_payload == {}
    assert inputs.macro_snapshot == {}
    assert inputs.social_payload == {}
    assert inputs.indicators_payload == {}
    # T1.6 — lots_summary now returns an explanatory sentinel when the
    # lots table is empty (helps the TaxAnalyst prompt understand the
    # absence rather than seeing an empty string). Same applies to
    # rsu_schedule_summary. dividends_summary stays empty for now —
    # there's no helper backfilling it yet.
    assert "no lots imported" in inputs.lots_summary or inputs.lots_summary == ""
    assert inputs.dividends_summary == ""
    assert (
        inputs.rsu_schedule_summary == ""
        or "rsu_grants" in inputs.rsu_schedule_summary
        or "no identity_yaml" in inputs.rsu_schedule_summary
    )


def test_assemble_uses_baseline_plan_label_and_markdown_when_present(
    tmp_path, monkeypatch,
):
    """When a baseline PlanVersion exists, its label + markdown flow into
    plan_label + plan_markdown."""
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        assemble_phase1_inputs,
    )

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    try:
        from argosy.config import reload_settings

        reload_settings()
    except Exception:
        pass

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    baseline = PlanVersion(
        user_id="ariel", role="baseline",
        version_label="my-baseline-v1",
        raw_markdown="# My Plan\n\nSome content.",
    )
    session.add(baseline)
    session.commit()
    session.refresh(baseline)

    inputs = assemble_phase1_inputs(
        session, user_id="ariel",
        baseline=baseline, prior_current=None,
        decision_audit_token="plan-synth-1",
    )
    assert inputs.plan_label == "my-baseline-v1"
    assert "My Plan" in inputs.plan_markdown


def test_assemble_never_raises_on_adapter_failure(tmp_path, monkeypatch):
    """The assembler must never raise from adapter failures — it must
    swallow + log + degrade to empty."""
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        Phase1Inputs,
        assemble_phase1_inputs,
    )

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    try:
        from argosy.config import reload_settings

        reload_settings()
    except Exception:
        pass

    # Force every adapter section to blow up loudly. If any of these
    # exceptions escape the assembler, the test fails.
    def _boom(*args, **kwargs):
        raise RuntimeError("synthetic adapter failure")

    monkeypatch.setattr(inputs_mod, "_gather_news", _boom)
    monkeypatch.setattr(inputs_mod, "_gather_macro_snapshot", _boom)
    monkeypatch.setattr(inputs_mod, "_gather_fx_payload", _boom)
    monkeypatch.setattr(inputs_mod, "_gather_social_payload", _boom)
    monkeypatch.setattr(inputs_mod, "_gather_indicators_payload", _boom)
    monkeypatch.setattr(inputs_mod, "_gather_fundamentals", _boom)
    monkeypatch.setattr(inputs_mod, "_find_latest_tsv", _boom)

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.commit()

    inputs = assemble_phase1_inputs(
        session, user_id="ariel",
        baseline=None, prior_current=None,
        decision_audit_token="plan-synth-99",
    )
    assert isinstance(inputs, Phase1Inputs)
    # All adapter-derived fields degrade to their empty default when
    # the section helpers raise.
    assert inputs.news_payload == {}
    assert inputs.macro_snapshot == {}
    assert inputs.fx_payload == {}
    assert inputs.social_payload == {}
    assert inputs.indicators_payload == {}
    assert inputs.fundamentals_payload == {}
    assert inputs.tickers == []


def test_indicators_payload_populates_from_yfinance(tmp_path, monkeypatch):
    """W3b.D: assemble_phase1_inputs calls _gather_indicators_payload for
    the discovered tickers and stores the per-ticker dict in
    indicators_payload."""
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        Phase1Inputs,
        assemble_phase1_inputs,
    )

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    try:
        from argosy.config import reload_settings

        reload_settings()
    except Exception:
        pass

    # Bypass TSV discovery: stub the helper so the assembler has
    # tickers to fan out over. Stub the other adapters so this test
    # only exercises the indicators wiring.
    def _fake_find_tsv():
        return None

    def _empty(*_a, **_kw):
        return {}

    captured: dict[str, list[str]] = {"calls": []}

    def _fake_indicators(tickers: list[str]) -> dict[str, dict[str, object]]:
        captured["calls"].append(list(tickers))
        return {
            t: {
                "price": 100.0 + i,
                "rsi_14": 55.5,
                "ma_50": 99.0,
                "ma_200": 95.0,
                "macd": 0.5,
                "macd_signal": 0.3,
                "ma_cross_50_200": "none",
                "atr_14": 1.2,
                "support": 90.0,
                "resistance": 110.0,
                "52w_high": 120.0,
                "52w_low": 80.0,
                "volume_avg": 1_000_000.0,
                "source": f"yfinance:{t}:1d",
            }
            for i, t in enumerate(tickers)
        }

    monkeypatch.setattr(inputs_mod, "_gather_news", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_macro_snapshot", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_fx_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_social_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_fundamentals", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_indicators_payload", _fake_indicators)

    # Drive tickers in by stubbing TSV-derived population: we directly
    # short-circuit the assembler's tickers by patching _find_latest_tsv
    # and _summarize_positions through a synthetic snapshot path. The
    # simplest way is to monkeypatch parse_portfolio_tsv on its import
    # path. But cleaner: also patch _find_latest_tsv to None so the TSV
    # branch is a no-op, then post-populate via the indicators helper
    # by forcing the assembler to see tickers another way.
    # The current assembler only sets `tickers` from the TSV path, so
    # we patch _find_latest_tsv to return a fake path AND patch
    # parse_portfolio_tsv to yield positions with our tickers.
    fake_path = tmp_path / "fake.tsv"
    fake_path.write_text("placeholder")
    monkeypatch.setattr(inputs_mod, "_find_latest_tsv", lambda: fake_path)

    class _FakePos:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            self.ticker = symbol
            self.quantity = 10
            self.market_value = 1000.0
            self.account = "test"

    class _FakeSnapshot:
        positions = [_FakePos("AAPL"), _FakePos("NVDA")]

    import argosy.ingest.tsv as tsv_mod

    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv", lambda _p: _FakeSnapshot()
    )

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.commit()

    inputs = assemble_phase1_inputs(
        session,
        user_id="ariel",
        baseline=None,
        prior_current=None,
        decision_audit_token="plan-synth-ind-1",
    )
    assert isinstance(inputs, Phase1Inputs)
    assert set(inputs.tickers) == {"AAPL", "NVDA"}
    assert set(inputs.indicators_payload.keys()) == {"AAPL", "NVDA"}
    for t in ("AAPL", "NVDA"):
        per_ticker = inputs.indicators_payload[t]
        # The keys advertised by TechnicalAnalystAgent.build_prompt all
        # appear in the per-ticker payload.
        for key in (
            "rsi_14", "macd", "macd_signal", "ma_50", "ma_200",
            "ma_cross_50_200", "atr_14", "support", "resistance", "source",
        ):
            assert key in per_ticker, f"{t} missing {key}"
        assert per_ticker["source"] == f"yfinance:{t}:1d"
    # The helper was driven once with the assembler's tickers list.
    assert captured["calls"], "indicators helper was never invoked"


def test_indicators_helper_skips_failing_tickers(monkeypatch):
    """_gather_indicators_payload skips per-ticker MissingDataSourceError
    and continues with the remaining tickers; logs an
    indicators_skipped warning."""
    from argosy.adapters import MissingDataSourceError
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod

    class _FakeAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_indicators(self, ticker: str) -> dict[str, object]:
            self.calls.append(ticker)
            if ticker == "BADT":
                raise MissingDataSourceError(
                    f"yfinance returned no history for {ticker}"
                )
            return {
                "price": 100.0,
                "rsi_14": 50.0,
                "macd": 0.1,
                "macd_signal": 0.05,
                "ma_50": 99.0,
                "ma_200": 95.0,
                "ma_cross_50_200": "none",
                "atr_14": 1.0,
                "support": 90.0,
                "resistance": 110.0,
                "52w_high": 120.0,
                "52w_low": 80.0,
                "volume_avg": 1_000_000.0,
                "source": f"yfinance:{ticker}:1d",
            }

    fake_adapter = _FakeAdapter()

    import argosy.adapters.data.yfinance_adapter as yf_mod

    monkeypatch.setattr(
        yf_mod, "YFinanceAdapter", lambda *_a, **_kw: fake_adapter
    )

    out = inputs_mod._gather_indicators_payload(["AAPL", "BADT", "NVDA"])
    # BADT was skipped, the rest populated.
    assert set(out.keys()) == {"AAPL", "NVDA"}
    assert fake_adapter.calls == ["AAPL", "BADT", "NVDA"]


def test_indicators_helper_caps_fanout_at_25(monkeypatch):
    """_gather_indicators_payload only calls the adapter for the first 25
    tickers (matches the news cap)."""
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod

    class _FakeAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_indicators(self, ticker: str) -> dict[str, object]:
            self.calls.append(ticker)
            return {"price": 1.0, "source": f"yfinance:{ticker}:1d"}

    fake_adapter = _FakeAdapter()

    import argosy.adapters.data.yfinance_adapter as yf_mod

    monkeypatch.setattr(
        yf_mod, "YFinanceAdapter", lambda *_a, **_kw: fake_adapter
    )

    tickers = [f"T{i:02d}" for i in range(40)]
    out = inputs_mod._gather_indicators_payload(tickers)
    assert len(fake_adapter.calls) == 25
    assert set(out.keys()) == set(tickers[:25])


# ----------------------------------------------------------------------
# W3b.E — Finnhub fundamentals wiring
# ----------------------------------------------------------------------


def test_fundamentals_payload_populates_from_finnhub(tmp_path, monkeypatch):
    """W3b.E: assemble_phase1_inputs calls _gather_fundamentals for the
    discovered tickers and stores the per-ticker dict in
    fundamentals_payload."""
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        Phase1Inputs,
        assemble_phase1_inputs,
    )

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    try:
        from argosy.config import reload_settings

        reload_settings()
    except Exception:
        pass

    def _empty(*_a, **_kw):
        return {}

    captured: dict[str, list[str]] = {"calls": []}

    def _fake_fundamentals(tickers: list[str]) -> dict[str, dict[str, object]]:
        captured["calls"].append(list(tickers))
        return {
            t: {
                "pe_ratio": 25.5,
                "pe_ratio_ttm": 24.9,
                "peg_ratio": 1.5,
                "ev_ebitda": 18.0,
                "eps_ttm": 4.2,
                "market_cap_m": 3_000_000.0,
                "revenue_growth_yoy": 0.12,
                "earnings_growth_yoy": 0.18,
                "gross_margin_ttm": 0.45,
                "operating_margin_ttm": 0.30,
                "debt_to_equity": 0.6,
                "dividend_yield": 0.005,
                "52w_high": 200.0,
                "52w_low": 130.0,
                "beta": 1.1,
                "source_url": f"https://finnhub.io/api/v1/stock/metric?symbol={t}",
            }
            for t in tickers
        }

    monkeypatch.setattr(inputs_mod, "_gather_news", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_macro_snapshot", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_fx_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_social_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_indicators_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_fundamentals", _fake_fundamentals)

    fake_path = tmp_path / "fake.tsv"
    fake_path.write_text("placeholder")
    monkeypatch.setattr(inputs_mod, "_find_latest_tsv", lambda: fake_path)

    class _FakePos:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            self.ticker = symbol
            self.quantity = 10
            self.market_value = 1000.0
            self.account = "test"

    class _FakeSnapshot:
        positions = [_FakePos("AAPL"), _FakePos("NVDA")]

    import argosy.ingest.tsv as tsv_mod

    monkeypatch.setattr(
        tsv_mod, "parse_portfolio_tsv", lambda _p: _FakeSnapshot()
    )

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.commit()

    inputs = assemble_phase1_inputs(
        session,
        user_id="ariel",
        baseline=None,
        prior_current=None,
        decision_audit_token="plan-synth-fund-1",
    )
    assert isinstance(inputs, Phase1Inputs)
    assert set(inputs.tickers) == {"AAPL", "NVDA"}
    assert set(inputs.fundamentals_payload.keys()) == {"AAPL", "NVDA"}
    for t in ("AAPL", "NVDA"):
        per_ticker = inputs.fundamentals_payload[t]
        for key in (
            "pe_ratio",
            "peg_ratio",
            "ev_ebitda",
            "revenue_growth_yoy",
            "earnings_growth_yoy",
            "debt_to_equity",
            "source_url",
        ):
            assert key in per_ticker, f"{t} missing {key}"
    assert captured["calls"], "fundamentals helper was never invoked"


def test_fundamentals_helper_skips_failing_tickers(monkeypatch):
    """_gather_fundamentals skips per-ticker MissingDataSourceError
    (typical for non-US listings) and continues with the rest."""
    from argosy.adapters import MissingDataSourceError
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod

    class _FakeAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_company_financials(self, ticker: str) -> dict[str, object]:
            self.calls.append(ticker)
            if ticker == "ILTB":  # synthetic non-US ticker
                raise MissingDataSourceError(
                    f"finnhub: empty metrics for {ticker}"
                )
            return {
                "pe_ratio": 20.0,
                "eps_ttm": 3.0,
                "market_cap_m": 1_000_000.0,
                "source_url": f"https://finnhub.io/api/v1/stock/metric?symbol={ticker}",
            }

    fake_adapter = _FakeAdapter()

    import argosy.adapters.data.finnhub_adapter as fh_mod

    monkeypatch.setattr(
        fh_mod, "FinnhubAdapter", lambda *_a, **_kw: fake_adapter
    )

    out = inputs_mod._gather_fundamentals(["AAPL", "ILTB", "NVDA"])
    # ILTB was skipped, AAPL + NVDA populated.
    assert set(out.keys()) == {"AAPL", "NVDA"}
    assert fake_adapter.calls == ["AAPL", "ILTB", "NVDA"]


def test_fundamentals_helper_aborts_on_missing_api_key(monkeypatch):
    """_gather_fundamentals stops iterating on MissingAPIKeyError (it's a
    global failure — no point retrying per ticker)."""
    from argosy.adapters import MissingAPIKeyError
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod

    class _FakeAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_company_financials(self, ticker: str) -> dict[str, object]:
            self.calls.append(ticker)
            raise MissingAPIKeyError(
                provider="Finnhub",
                keychain_key="argosy.finnhub.api_key",
                env_var="FINNHUB_API_KEY",
            )

    fake_adapter = _FakeAdapter()

    import argosy.adapters.data.finnhub_adapter as fh_mod

    monkeypatch.setattr(
        fh_mod, "FinnhubAdapter", lambda *_a, **_kw: fake_adapter
    )

    out = inputs_mod._gather_fundamentals(["AAPL", "NVDA", "MSFT"])
    # First ticker triggers the key error -> loop aborts; no later
    # tickers are attempted.
    assert out == {}
    assert fake_adapter.calls == ["AAPL"]


def test_fundamentals_helper_caps_fanout_at_25(monkeypatch):
    """_gather_fundamentals only calls the adapter for the first 25
    tickers (matches the news cap)."""
    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod

    class _FakeAdapter:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_company_financials(self, ticker: str) -> dict[str, object]:
            self.calls.append(ticker)
            return {
                "pe_ratio": 20.0,
                "source_url": f"https://finnhub.io/api/v1/stock/metric?symbol={ticker}",
            }

    fake_adapter = _FakeAdapter()

    import argosy.adapters.data.finnhub_adapter as fh_mod

    monkeypatch.setattr(
        fh_mod, "FinnhubAdapter", lambda *_a, **_kw: fake_adapter
    )

    tickers = [f"T{i:02d}" for i in range(40)]
    out = inputs_mod._gather_fundamentals(tickers)
    assert len(fake_adapter.calls) == 25
    assert set(out.keys()) == set(tickers[:25])


# ----------------------------------------------------------------------
# NVDA YTD pace — assembler must populate nvda_shares_sold_ytd /
# nvda_target_shares_ytd when fills + a plan target exist.
#
# Before this wiring, Phase1Inputs declared those fields but nothing ever
# set them; ConcentrationAnalystAgent then emitted shares_sold_ytd=0 in
# every synthesis report and the home widget showed "0 / 10,000 BEHIND
# PACE" forever.
# ----------------------------------------------------------------------


def test_nvda_shares_sold_ytd_populates_from_fills(tmp_path, monkeypatch):
    """A NVDA SELL fill in the current year flows into
    Phase1Inputs.nvda_shares_sold_ytd via the assembler.
    """
    from datetime import datetime, timezone
    from decimal import Decimal

    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        assemble_phase1_inputs,
    )
    from argosy.state.models import Fill

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    try:
        from argosy.config import reload_settings

        reload_settings()
    except Exception:
        pass

    # Silence the network-touching adapters so we only exercise the NVDA
    # YTD wiring (same defensive shape the other assembler tests use).
    def _empty(*_a, **_kw):
        return {}

    monkeypatch.setattr(inputs_mod, "_gather_news", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_macro_snapshot", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_fx_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_social_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_indicators_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_fundamentals", _empty)
    monkeypatch.setattr(inputs_mod, "_find_latest_tsv", lambda: None)

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.add(
        Fill(
            user_id="ariel",
            broker="schwab",
            broker_order_id="x1",
            ticker="NVDA",
            action="SELL",
            quantity=Decimal("520"),
            price=Decimal("199"),
            commission=Decimal("0"),
            filled_at=datetime.now(timezone.utc).replace(month=2, day=15),
            paper=False,
        )
    )
    session.commit()

    inputs = assemble_phase1_inputs(
        session,
        user_id="ariel",
        baseline=None,
        prior_current=None,
        decision_audit_token="plan-synth-nvda",
    )
    assert inputs.nvda_shares_sold_ytd == 520, (
        f"expected 520 shares sold YTD via fills, got "
        f"{inputs.nvda_shares_sold_ytd}"
    )


def test_nvda_target_shares_ytd_populates_from_draft(tmp_path, monkeypatch):
    """An active draft with a horizon_medium NVDA-sale target flows into
    Phase1Inputs.nvda_target_shares_ytd via the assembler.
    """
    import json
    from datetime import date

    from argosy.orchestrator.flows.plan_synthesis import inputs as inputs_mod
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        assemble_phase1_inputs,
    )

    monkeypatch.setenv("ARGOSY_HOME", str(tmp_path))

    def _empty(*_a, **_kw):
        return {}

    monkeypatch.setattr(inputs_mod, "_gather_news", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_macro_snapshot", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_fx_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_social_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_indicators_payload", _empty)
    monkeypatch.setattr(inputs_mod, "_gather_fundamentals", _empty)
    monkeypatch.setattr(inputs_mod, "_find_latest_tsv", lambda: None)

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.add(
        PlanVersion(
            user_id="ariel",
            role="draft",
            version_label="t",
            horizon_medium_json=json.dumps({
                "targets": [
                    {
                        "label": "NVDA deconcentration shares to sell (next 12 months)",
                        "value": 1440.0,
                        "unit": "shares",
                    }
                ],
            }),
        )
    )
    session.commit()

    inputs = assemble_phase1_inputs(
        session,
        user_id="ariel",
        baseline=None,
        prior_current=None,
        decision_audit_token="plan-synth-nvda-target",
    )
    # Target should be a sensible pro-rata of 1440 (i.e. > 0 and <= 1440).
    # We don't pin to as_of because the assembler reads "today" from the
    # service helper — assert the structural invariant instead.
    assert 0 < inputs.nvda_target_shares_ytd <= 1440, (
        f"expected pro-rated target in (0, 1440], got "
        f"{inputs.nvda_target_shares_ytd}"
    )
    # And confirm the value is in the right *order of magnitude* for
    # the typical synth-day (about 30%-90% of 1440 between Apr-Nov).
    today = date.today()
    days = (today - date(today.year, 1, 1)).days + 1
    expected = round(1440 * days / 365.0)
    assert abs(inputs.nvda_target_shares_ytd - expected) <= 2


# ----------------------------------------------------------------------
# _assemble_fills_summary — was a placeholder string ("(fills summary —
# wired against fills + proposals tables)") that the synthesizer Phase 3
# read at every run. Same bug class as T1.1's _assemble_portfolio_summary
# stub. The helper now reads from the ``fills`` table + the
# ``decision_runs`` table (status='completed' + fund_manager_decision=
# 'approved' + decision_kind in ('trade_proposal','plan_revision')) and
# emits a per-line summary that the synthesizer's user prompt section
# "=== RECENT FILLS + DECISIONS (last 90 days) ===" can render.
# ----------------------------------------------------------------------


def test_assemble_fills_summary_includes_real_fills():
    """A BUY + a SELL fill in the last 90 days, plus an approved decision
    run, all appear in the assembled summary text; the YTD footer counts
    realized gains derived from the lots table."""
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        _assemble_fills_summary,
    )
    from argosy.state.models import DecisionRun, Fill, Lot

    session = _make_session()
    session.add(User(id="ariel", plan="free"))

    now = datetime.now(timezone.utc)
    # A lot for NVDA giving an avg cost basis of $150/share so the
    # realized gain on the SELL is unambiguous.
    session.add(
        Lot(
            user_id="ariel",
            account_id="schwab-x",
            ticker="NVDA",
            quantity=Decimal("100"),
            cost_basis_usd=Decimal("15000"),
            acquired_at=now.replace(year=now.year - 1),
            source="schwab_csv",
        )
    )
    # SELL fill 10 days ago at $200 → realized = (200 - 150) * 10 = $500.
    session.add(
        Fill(
            user_id="ariel",
            broker="schwab",
            broker_order_id="o-sell-1",
            ticker="NVDA",
            action="SELL",
            quantity=Decimal("10"),
            price=Decimal("200"),
            commission=Decimal("0"),
            filled_at=now.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=10),
            paper=False,
        )
    )
    # BUY fill 30 days ago — should render but contribute no realized gain.
    session.add(
        Fill(
            user_id="ariel",
            broker="schwab",
            broker_order_id="o-buy-1",
            ticker="AAPL",
            action="BUY",
            quantity=Decimal("5"),
            price=Decimal("180"),
            commission=Decimal("0"),
            filled_at=now.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=30),
            paper=False,
        )
    )
    # An approved decision run inside the 90-day window.
    session.add(
        DecisionRun(
            user_id="ariel",
            ticker="NVDA",
            tier="T2",
            started_at=now - timedelta(days=12),
            finished_at=now - timedelta(days=11),
            status="completed",
            fund_manager_decision="approved",
            decision_kind="trade_proposal",
        )
    )
    # Negative controls: a rejected run + an out-of-window approved run —
    # neither should appear.
    session.add(
        DecisionRun(
            user_id="ariel",
            ticker="MSFT",
            tier="T1",
            started_at=now - timedelta(days=5),
            finished_at=now - timedelta(days=4),
            status="completed",
            fund_manager_decision="rejected",
            decision_kind="trade_proposal",
        )
    )
    session.add(
        DecisionRun(
            user_id="ariel",
            ticker="GOOG",
            tier="T3",
            started_at=now - timedelta(days=200),
            finished_at=now - timedelta(days=199),
            status="completed",
            fund_manager_decision="approved",
            decision_kind="trade_proposal",
        )
    )
    session.commit()

    text = _assemble_fills_summary(session=session, user_id="ariel")

    # The SELL fill appears, with the realized gain we expect.
    assert "NVDA" in text, text
    assert "side=SELL" in text, text
    assert "qty=10" in text, text
    assert "price=$200" in text, text
    # The realized gain ($500.00) is computed against the avg-cost lot.
    assert "realized_gain_usd=$500.00" in text, text
    # The BUY fill is rendered with realized_gain_usd=n/a (no basis).
    assert "AAPL" in text, text
    assert "side=BUY" in text, text
    # The approved decision shows up; rejected + out-of-window don't.
    assert "approved tier=T2" in text, text
    assert "/decisions/" in text, text
    assert "MSFT" not in text, text
    assert "GOOG" not in text, text
    # YTD footer reflects exactly the one realized SELL.
    # (The SELL is 10 days ago; safe assumption it's in the current
    # calendar year for any plausible test-run date.)
    assert "Total realized YTD: $500.00 across 1 fills" in text, text


def test_assemble_fills_summary_empty_when_no_data():
    """Empty fills + empty decision_runs → stable sentinel string the
    synthesizer's user prompt can read without falling through to "null
    data" objections from Fund Manager.
    """
    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        _assemble_fills_summary,
    )

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.commit()

    text = _assemble_fills_summary(session=session, user_id="ariel")
    assert text == "(no recent fills or accepted decisions in last 90 days)"


# ----------------------------------------------------------------------
# Tax / treaties domain-KB loader
# ----------------------------------------------------------------------
# Wave 5: TaxAnalystAgent.build_prompt declares domain_kb_files as
# "Mandatory input — citation-gate fails without these." The inputs
# assembler was leaving the field empty (with a wrong comment claiming
# each analyst pulls its own files), so Tax silently failed citations
# for three consecutive plan-revision cycles. These tests pin the
# loader so the regression doesn't repeat.


def test_load_tax_domain_kb_files_returns_every_markdown_under_tax(tmp_path, monkeypatch):
    """Walks ARGOSY_HOME/domain_knowledge/tax/ recursively, returns every
    `.md` file as { repo-relative-path: contents }."""
    home = tmp_path / "fake_argosy"
    tax = home / "domain_knowledge" / "tax"
    (tax / "israel" / "retirement").mkdir(parents=True)
    (tax / "us").mkdir()
    (tax / "israel" / "capital_gains.md").write_text("CG content", encoding="utf-8")
    (tax / "israel" / "retirement" / "section_102.md").write_text(
        "S102 content", encoding="utf-8"
    )
    (tax / "us" / "estate_tax_nonresidents.md").write_text(
        "estate content", encoding="utf-8"
    )
    # Non-markdown file → should be skipped, never appears in the dict.
    (tax / "README.txt").write_text("ignored", encoding="utf-8")

    monkeypatch.setenv("ARGOSY_HOME", str(home))
    from argosy.config import reload_settings

    reload_settings()

    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        _load_tax_domain_kb_files,
    )

    out = _load_tax_domain_kb_files()
    assert set(out.keys()) == {
        "domain_knowledge/tax/israel/capital_gains.md",
        "domain_knowledge/tax/israel/retirement/section_102.md",
        "domain_knowledge/tax/us/estate_tax_nonresidents.md",
    }
    assert out["domain_knowledge/tax/israel/capital_gains.md"] == "CG content"
    assert out["domain_knowledge/tax/us/estate_tax_nonresidents.md"] == "estate content"


def test_load_tax_domain_kb_files_returns_empty_when_dir_missing(tmp_path, monkeypatch):
    """No ``domain_knowledge/tax/`` directory → empty dict, no raise."""
    home = tmp_path / "fake_argosy"
    home.mkdir()
    # Deliberately do NOT create the domain_knowledge dir.

    monkeypatch.setenv("ARGOSY_HOME", str(home))
    from argosy.config import reload_settings

    reload_settings()

    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        _load_tax_domain_kb_files,
    )

    assert _load_tax_domain_kb_files() == {}


def test_assemble_phase1_inputs_populates_domain_kb_files_from_tax_dir(
    tmp_path, monkeypatch
):
    """End-to-end: a real-shaped ARGOSY_HOME with tax markdown produces
    a non-empty inputs.domain_kb_files. Regression: the field was being
    left empty despite files existing on disk."""
    home = tmp_path / "fake_argosy"
    tax = home / "domain_knowledge" / "tax" / "israel"
    tax.mkdir(parents=True)
    (tax / "capital_gains.md").write_text("CG content for test", encoding="utf-8")

    monkeypatch.setenv("ARGOSY_HOME", str(home))
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    from argosy.config import reload_settings

    reload_settings()

    from argosy.orchestrator.flows.plan_synthesis.inputs import (
        assemble_phase1_inputs,
    )

    session = _make_session()
    session.add(User(id="ariel", plan="free"))
    session.commit()

    inputs = assemble_phase1_inputs(
        session,
        user_id="ariel",
        baseline=None,
        prior_current=None,
        decision_audit_token="plan-synth-42",
    )
    # The field MUST be populated — TaxAnalystAgent.build_prompt requires it.
    assert "domain_knowledge/tax/israel/capital_gains.md" in inputs.domain_kb_files
    assert (
        inputs.domain_kb_files["domain_knowledge/tax/israel/capital_gains.md"]
        == "CG content for test"
    )

"""The period directive — the team's ONE assembled 'here's your move' object.

Composes the buy half (idle-cash → canonical engine, incl. discovery sleeve) and
the sell half (glide policy sell) into a single verdict with a freshness stamp, so
both the inbox card and the Step-3 loop render the same directive. Read-only unless
``refresh=True`` (the on-demand 'wait while I refresh stale inputs' path)."""
from __future__ import annotations

import datetime as _dt

import argosy.services.period_directive as pd
from argosy.services.nvda_policy_sell import NvdaPolicySell
from argosy.services.period_directive import assemble_period_directive

_TODAY = _dt.date(2026, 7, 2)


def _sell(status):
    if status == "sell_due":
        return NvdaPolicySell(
            status="sell_due", category="policy", tranche_nis=500_000.0,
            nvda_current_pct=57.0, nvda_cap_pct=13.0, n_quarters=8,
            headline="Trim NVDA ~₪500,000 this quarter.", tax_note="CGT note.",
        )
    return NvdaPolicySell(
        status="no_action", category="policy", tranche_nis=0.0,
        nvda_current_pct=0.0, nvda_cap_pct=0.0, n_quarters=0,
        headline="NVDA within its cap.", tax_note="",
    )


class _Ev:
    def __init__(self, excess):
        self.excess_usd = excess
        self.headline = "Cash sits above your plan target."
        self.snapshot_date = "2026-06-30"
        self.proposals = []


def test_composes_buy_and_sell_when_both_due(monkeypatch):
    monkeypatch.setattr(pd, "_detect_cash", lambda db, u, t: _Ev(30_000.0))
    monkeypatch.setattr(
        pd, "build_buy_list",
        lambda db, u, excess, t: [{"instrument": "CSPX", "asset_class": "core",
                                   "amount_usd": excess, "tier": "core", "rationale": "gap"}],
    )
    monkeypatch.setattr(pd, "_assess_sell", lambda db, u, t: _sell("sell_due"))

    d = assemble_period_directive(db=object(), user_id="ariel", today=_TODAY)

    assert d.has_actions is True
    assert d.buy_excess_usd == 30_000.0
    assert d.buy and d.buy[0]["instrument"] == "CSPX"
    assert d.sell.status == "sell_due"
    assert d.generated_at  # stamped


def test_quiet_when_nothing_due(monkeypatch):
    monkeypatch.setattr(pd, "_detect_cash", lambda db, u, t: None)
    monkeypatch.setattr(pd, "_assess_sell", lambda db, u, t: _sell("no_action"))

    d = assemble_period_directive(db=object(), user_id="ariel", today=_TODAY)

    assert d.has_actions is False
    assert d.buy == []
    assert d.buy_excess_usd == 0.0
    assert d.sell.status == "no_action"


def test_refresh_true_refreshes_stale_fx_before_advising(monkeypatch):
    """The on-demand path refreshes stale inputs first — never advise on stale
    data — and records that it did."""
    calls = {"fx": 0}

    def _fx(db):
        calls["fx"] += 1
        return True  # a refresh was performed

    monkeypatch.setattr(pd, "_refresh_fx", _fx)
    monkeypatch.setattr(pd, "_fx_is_stale", lambda db: False)
    monkeypatch.setattr(pd, "_detect_cash", lambda db, u, t: None)
    monkeypatch.setattr(pd, "_assess_sell", lambda db, u, t: _sell("no_action"))

    d = assemble_period_directive(db=object(), user_id="ariel", today=_TODAY, refresh=True)

    assert calls["fx"] == 1
    assert d.freshness["refreshed"] is True


def test_to_dict_is_json_projection(monkeypatch):
    monkeypatch.setattr(pd, "_detect_cash", lambda db, u, t: _Ev(30_000.0))
    monkeypatch.setattr(
        pd, "build_buy_list",
        lambda db, u, excess, t: [{"instrument": "CSPX", "asset_class": "core",
                                   "amount_usd": excess, "tier": "core", "rationale": "gap"}],
    )
    monkeypatch.setattr(pd, "_assess_sell", lambda db, u, t: _sell("sell_due"))

    d = assemble_period_directive(db=object(), user_id="ariel", today=_TODAY).to_dict()

    assert d["has_actions"] is True
    assert d["buy"]["excess_usd"] == 30_000.0
    assert d["buy"]["items"][0]["instrument"] == "CSPX"
    assert d["sell"]["status"] == "sell_due"
    assert d["sell"]["tranche_nis"] == 500_000.0
    assert "freshness" in d and "generated_at" in d


def test_refresh_false_does_not_touch_fx(monkeypatch):
    calls = {"fx": 0}
    monkeypatch.setattr(pd, "_refresh_fx", lambda db: calls.__setitem__("fx", calls["fx"] + 1))
    monkeypatch.setattr(pd, "_detect_cash", lambda db, u, t: None)
    monkeypatch.setattr(pd, "_assess_sell", lambda db, u, t: _sell("no_action"))

    assemble_period_directive(db=object(), user_id="ariel", today=_TODAY, refresh=False)

    assert calls["fx"] == 0


def test_refresh_failure_flags_fx_stale_not_silent(monkeypatch):
    """If an on-demand FX refresh FAILS (or the fetch returns without freshening),
    the directive reads ACTUAL cache staleness — it must flag fx_stale, not infer
    'clean' from the best-effort refresh returning False."""
    def _boom(db):
        raise RuntimeError("BoI unreachable")

    monkeypatch.setattr(pd, "_refresh_fx", _boom)
    monkeypatch.setattr(pd, "_fx_is_stale", lambda db: True)  # cache still stale
    monkeypatch.setattr(pd, "_detect_cash", lambda db, u, t: None)
    monkeypatch.setattr(pd, "_assess_sell", lambda db, u, t: _sell("no_action"))

    d = assemble_period_directive(db=object(), user_id="ariel", today=_TODAY, refresh=True)

    assert d.freshness["refreshed"] is False
    assert d.freshness["fx_stale"] is True


def test_fx_stale_flagged_from_cache_state_even_when_refresh_reports_done(monkeypatch):
    """The flag reflects the cache, not the refresh boolean: a refresh that reports
    False-but-fresh is not stale; a stale cache is flagged regardless."""
    monkeypatch.setattr(pd, "_refresh_fx", lambda db: True)
    monkeypatch.setattr(pd, "_fx_is_stale", lambda db: False)
    monkeypatch.setattr(pd, "_detect_cash", lambda db, u, t: None)
    monkeypatch.setattr(pd, "_assess_sell", lambda db, u, t: _sell("no_action"))

    d = assemble_period_directive(db=object(), user_id="ariel", today=_TODAY, refresh=True)
    assert d.freshness["fx_stale"] is False
    assert d.freshness["refreshed"] is True


def test_fx_stale_read_error_fails_closed(monkeypatch):
    """If the freshness read itself errors, the directive must NOT report clean —
    unknown freshness is treated as stale (fail-closed, never silently clean)."""
    def _boom(db):
        raise RuntimeError("fx table unreadable")

    monkeypatch.setattr(pd, "_fx_is_stale", _boom)
    monkeypatch.setattr(pd, "_detect_cash", lambda db, u, t: None)
    monkeypatch.setattr(pd, "_assess_sell", lambda db, u, t: _sell("no_action"))

    d = assemble_period_directive(db=object(), user_id="ariel", today=_TODAY)

    assert d.freshness["fx_stale"] is True


def test_detect_cash_error_degrades_gracefully(monkeypatch):
    """A detector exception (e.g. a malformed stored snapshot) must NOT 500 the
    directive — it degrades to no buy half."""
    def _boom(db, u, t):
        raise ValueError("malformed snapshot json")

    monkeypatch.setattr(pd, "_detect_cash", _boom)
    monkeypatch.setattr(pd, "_assess_sell", lambda db, u, t: _sell("no_action"))

    d = assemble_period_directive(db=object(), user_id="ariel", today=_TODAY)

    assert d.buy == []
    assert d.buy_excess_usd == 0.0
    assert d.sell.status == "no_action"


def test_assess_sell_error_degrades_to_no_action(monkeypatch):
    def _boom(db, u, t):
        raise RuntimeError("resolver blew up")

    monkeypatch.setattr(pd, "_detect_cash", lambda db, u, t: None)
    monkeypatch.setattr(pd, "_assess_sell", _boom)

    d = assemble_period_directive(db=object(), user_id="ariel", today=_TODAY)

    assert d.sell.status == "no_action"


def test_discovery_stale_days_parses_iso_datetime_string(monkeypatch):
    """_load_discovery_state returns an ISO *string* for `last`; the staleness
    calc must parse it (not TypeError into a swallowed None)."""
    import argosy.api.routes.portfolio as port

    monkeypatch.setattr(
        port, "_load_discovery_state",
        lambda uid: ([], [], "2026-06-25T10:00:00+00:00"),
    )
    days = pd._discovery_stale_days(object(), "ariel", _dt.date(2026, 7, 2))
    assert days == 7


def test_route_returns_quiet_directive_on_empty_db(client_with_db):
    """The endpoint always returns a well-formed directive; an empty DB (no plan /
    snapshot) is the quiet steady state, not an error."""
    r = client_with_db.get("/api/period-directive?user_id=ariel")
    assert r.status_code == 200
    body = r.json()
    assert body["has_actions"] is False
    assert body["buy"]["items"] == []
    assert body["sell"]["status"] == "no_action"
    assert "freshness" in body

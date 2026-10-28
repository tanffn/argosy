from copy import deepcopy

import pytest

from argosy.services.shadow_accounting import apply_sheet, mark, new_book, replay, session_actions


def seed():
    return {"activation_date": "2026-01-01", "positions": {"bank|AAA": {
        "instrument": "AAA", "account": "bank", "currency": "USD", "quantity": "100",
        "lots": [{"lot_id": "lot", "quantity": "100", "cost_basis_usd": "500"}]}},
        "cash": {"bank|USD": {"account": "bank", "currency": "USD", "quantity": "1000"}}}


def market(day, aaa="10", bbb="20", spy="100", actions=()):
    return {"date": day, "fx": {"USD": "1"}, "actions": list(actions), "marks": {
        key: {"price": price, "currency": "USD", "as_of": day, "source": "fixture:raw", "eligible": True}
        for key, price in (("AAA", aaa), ("BBB", bbb), ("SPY", spy))}}


def receipt(lines=None, **kwargs):
    return {"receipt_id": "one", "intent_id": "intent-one", "published_at": "2026-01-02T22:00:00+00:00",
        "validation": {"valid": True}, "sheet": {"lines": lines or [
            {"symbol": "AAA", "action": "TRIM", "shares": 50, "instrument_type": "stock"},
            {"symbol": "BBB", "action": "BUY", "shares": 50, "instrument_type": "stock"}]},
        "accounts": {"AAA": "bank", "BBB": "bank"}, "instruments": {"AAA": "AAA", "BBB": "BBB"},
        "economics": {"AAA": {"fee_usd": 1, "basis_contract": "verified_usd_hifo", "tax_policy": {
            "effective_tax_rate": .25, "method": "fixture-compatible", "source": "fixture-policy",
            "authoritative": True}}, "BBB": {"fee_usd": 2}}, **kwargs}


def test_whole_book_cash_sells_gross_net_and_matched_benchmark():
    result = replay(seed(), [market("2026-01-02"), market("2026-01-05"),
                             market("2026-01-06", bbb="30", spy="110")], [receipt()])
    latest = result["history"][-1]["books"]
    assert float(latest["gross"]["unchanged"]["nav_usd"]) == 2000
    assert float(latest["gross"]["argosy"]["nav_usd"]) == 2500
    assert float(latest["gross"]["spy"]["nav_usd"]) == 2200
    assert float(latest["gross"]["argosy"]["return_pct"]) == pytest.approx(25)
    assert float(latest["net"]["argosy"]["nav_usd"]) == 2434.5
    assert float(latest["net"]["argosy"]["taxes_usd"]) == 62.5
    assert float(latest["net"]["argosy"]["fees_usd"]) == 3
    assert result == replay(seed(), [market("2026-01-02"), market("2026-01-05"),
                                    market("2026-01-06", bbb="30", spy="110")], [receipt()])


def test_unfunded_sheet_never_partially_sells_or_resizes():
    book = new_book(seed())
    original = deepcopy(book)
    raw = receipt()
    raw["sheet"]["lines"][1]["shares"] = 1000
    applied, reasons, fills = apply_sheet(book, raw, market("2026-01-05"))
    assert applied is None and reasons and not fills
    assert book == original


def test_split_between_authored_quote_and_execution_requires_revised_units():
    packet = market("2026-01-05", actions=[{"id": "split", "instrument": "AAA",
                    "kind": "split", "ratio": "2", "ex_date": "2026-01-05"}])
    applied, reasons, _ = apply_sheet(new_book(seed()), receipt(), packet)
    assert applied is None
    assert any("authored share units" in reason for reason in reasons)


def test_unknown_sale_quantity_is_a_reported_gap_not_a_crash():
    book = new_book(seed())
    book["positions"]["bank|AAA"]["quantity"] = None
    applied, reasons, _ = apply_sheet(book, receipt(), market("2026-01-05"))
    assert applied is None
    assert any("inventory insufficient" in reason for reason in reasons)


def test_unknown_fees_only_block_net_and_never_become_zero():
    raw = receipt()
    raw["economics"] = {}
    result = replay(seed(), [market("2026-01-02"), market("2026-01-05")], [raw])
    end = result["history"][-1]["books"]
    assert end["gross"]["argosy"]["return_pct"] is not None
    assert end["net"]["argosy"]["after_tax_nav_usd"] is None
    assert [r for r in result["applications"] if r.get("mode") == "net"][0]["status"] == "blocked"


def test_missing_held_price_blocks_full_nav_not_reduced_denominator():
    packet = market("2026-01-05")
    packet["marks"].pop("AAA")
    value = mark(new_book(seed()), packet, packet["date"])
    assert value["nav_usd"] is None
    assert value["covered_usd"] == "1000"
    assert value["missing"] == ["bank|AAA"]


@pytest.mark.parametrize("sell", [False, True])
def test_action_history_gap_persists_into_future_nav_and_cannot_be_sold_away(sell):
    gap = market("2026-01-05", aaa="5")
    gap["unresolved_actions"] = ["AAA"]
    later = market("2026-01-06", aaa="5")
    orders = [receipt(lines=[{"symbol": "AAA", "action": "SELL", "shares": 100}])] if sell else []
    result = replay(seed(), [market("2026-01-02"), gap, later], orders)
    for day in result["history"][1:]:
        for book in day["books"]["gross"].values():
            if book is day["books"]["gross"]["spy"]:
                continue
            assert book["nav_usd"] is None and book["return_pct"] is None
    if sell:
        assert any("corporate-action" in reason for row in result["applications"] for reason in row.get("failures", []))


def test_missing_initial_price_does_not_move_baseline_to_later_favorable_day():
    first = market("2026-01-02")
    first["marks"].pop("AAA")
    result = replay(seed(), [first, market("2026-01-05", aaa="20")])
    assert result["baseline_date"] is None
    assert result["history"][0]["status"] == "baseline_incomplete"


def test_same_intent_republication_and_explicit_economic_duplicates_do_not_rebuy():
    first = receipt(execution_key="economic-one")
    repeat = receipt(receipt_id="two", published_at="2026-01-06T22:00:00+00:00")
    other = receipt(receipt_id="three", intent_id="another", execution_key="economic-one",
                    published_at="2026-01-07T22:00:00+00:00")
    result = replay(seed(), [market(day) for day in ("2026-01-02", "2026-01-05", "2026-01-07", "2026-01-08")],
                    [first, repeat, other])
    assert len([row for row in result["applications"] if row.get("status") == "applied"]) == 2
    assert len([row for row in result["applications"] if row["status"] == "refresh_not_an_additional_trade"]) == 2


def test_same_close_external_cash_flows_are_not_investment_returns():
    result = replay(seed(), [market("2026-01-02"), market("2026-01-05")], flows=[{
        "date": "2026-01-05", "account": "bank", "currency": "USD", "amount": "1000"}])
    for mode in result["history"][-1]["books"].values():
        assert all(float(book["nav_usd"]) == 3000 for book in mode.values())
        assert all(float(book["return_pct"]) == 0 for book in mode.values())


def test_pre_baseline_deposit_is_opening_capital_not_lost_or_a_gain():
    result = replay(seed(), [market("2026-01-02"), market("2026-01-05")], flows=[{
        "date": "2026-01-01", "account": "bank", "currency": "USD", "amount": "500"}])
    assert float(result["starting_nav_usd"]) == 2500
    assert all(float(row["return_pct"]) == 0 for row in result["history"][-1]["books"]["gross"].values())


@pytest.mark.parametrize("missing", ["SPY", "FX"])
def test_missing_flow_valuation_publishes_incomplete_books_instead_of_crashing(missing):
    packet = market("2026-01-05")
    if missing == "SPY":
        packet["marks"].pop("SPY")
    result = replay(seed(), [market("2026-01-02"), packet], flows=[{
        "date": "2026-01-05", "account": "bank", "currency": "EUR" if missing == "FX" else "USD", "amount": "500"}])
    assert result["status"] == "partial"
    assert result["history"][-1]["books"]["gross"]["spy"]["return_pct"] is None


def test_deposit_after_london_close_cannot_fund_earlier_purchase():
    book = new_book(seed())
    book["cash"]["bank|USD"]["quantity"] = "5000"
    raw = receipt(lines=[{"symbol": "BBB", "action": "BUY", "shares": 200}])
    packet = market("2026-01-05")
    packet["marks"]["BBB"]["close_at"] = "2026-01-05T16:30:00+00:00"
    applied, failures, _ = apply_sheet(book, raw, packet, inflows=[{
        "account": "bank", "currency": "USD", "amount": "5000", "effective_at": "2026-01-05T20:00:00+00:00"}])
    assert applied is None and any("cash insufficient" in reason for reason in failures)


def test_pre_baseline_source_cannot_be_repackaged_as_new_aggregate_intent():
    old = receipt(published_at="2026-01-01T20:00:00+00:00", source_intents=["recommendation:A"])
    new = receipt(receipt_id="new", intent_id="A-and-B", source_intents=["recommendation:A", "recommendation:B"])
    result = replay(seed(), [market("2026-01-02"), market("2026-01-05")], [old, new])
    assert not [row for row in result["applications"] if row.get("mode") == "gross" and row["status"] == "applied"]
    assert result["status"] == "partial"


def test_splits_preserve_economic_value_lot_basis_and_dividend_entitlement():
    book = new_book(seed())
    split = {"id": "split", "kind": "split", "instrument": "AAA", "ratio": "2"}
    dividend = {"id": "div", "kind": "distribution", "instrument": "AAA", "amount": "1", "pay_date": None}
    packet = market("2026-01-05", aaa="5", actions=[split, dividend])
    session_actions(book, packet)
    assert book["positions"]["bank|AAA"]["quantity"] == "200"
    assert book["positions"]["bank|AAA"]["lots"][0]["cost_basis_usd"] == "500"
    assert book["cash"]["bank|USD"]["quantity"] == "1000"
    assert book["receivables"][0]["amount"] == "200"
    session_actions(book, packet)
    assert len(book["receivables"]) == 1
    assert book["positions"]["bank|AAA"]["quantity"] == "200"


def test_new_close_buy_on_ex_date_does_not_earn_distribution():
    dividend = {"id": "div", "kind": "distribution", "instrument": "BBB", "amount": "1", "pay_date": None}
    result = replay(seed(), [market("2026-01-02"), market("2026-01-05", actions=[dividend])], [receipt()])
    assert not result["books"]["gross"]["argosy"]["receivables"]


def test_omitted_spy_session_cannot_shift_baseline_or_execution():
    assert replay(seed(), [market("2026-01-05")])["baseline_date"] is None
    result = replay(seed(), [market("2026-01-02"), market("2026-01-06")], [receipt()])
    first_attempt = [r for r in result["applications"] if r.get("mode") == "gross"][0]
    assert first_attempt["date"] == "2026-01-05"
    assert first_attempt["status"] == "blocked"


def test_pre_baseline_advice_cannot_be_reset_by_a_later_refresh():
    original = receipt(published_at="2026-01-02T12:00:00+00:00")
    refresh = receipt(receipt_id="later", published_at="2026-01-05T22:00:00+00:00")
    result = replay(seed(), [market("2026-01-02"), market("2026-01-05"), market("2026-01-06")], [original, refresh])
    assert not [r for r in result["applications"] if r["status"] == "applied"
               and r.get("mode") == "gross"]


def test_partially_overlapping_source_intents_do_not_replay_old_trades():
    first = receipt(source_intents=["A"])
    second = receipt(receipt_id="two", intent_id="A-plus-B", source_intents=["A", "B"],
                     published_at="2026-01-05T22:00:00+00:00")
    result = replay(seed(), [market("2026-01-02"), market("2026-01-05"), market("2026-01-06")], [first, second])
    blocked = [r for r in result["applications"] if r["receipt_id"] == "two" and r.get("mode") == "gross"]
    assert blocked[0]["status"] == "blocked"
    assert "explicit prospective" in blocked[0]["failures"][0]


@pytest.mark.parametrize("bad", [{"fee_usd": -30}, {"fee_usd": float("inf")},
                                  {"fee_usd": 0, "tax_policy": {}}, ["not", "economics"]])
def test_invalid_net_economics_cannot_break_or_subsidize_gross(bad):
    raw = receipt()
    raw["economics"]["AAA"] = bad
    result = replay(seed(), [market("2026-01-02"), market("2026-01-05")], [raw])
    applied = [r for r in result["applications"] if r.get("mode") == "gross"]
    assert applied[0]["status"] == "applied"
    assert result["history"][-1]["books"]["net"]["argosy"]["after_tax_nav_usd"] is None


def test_baseline_withholding_is_not_a_negative_baseline_return():
    original = seed()
    original["dividend_tax_rate"] = ".25"
    distribution = {"id": "opening", "kind": "distribution", "instrument": "AAA", "amount": "1",
                    "classification": "ordinary_cash", "pay_date": None}
    result = replay(original, [market("2026-01-02", actions=[distribution])])
    for mode in result["history"][0]["books"].values():
        assert all(float(book["return_pct"]) == 0 for book in mode.values())


def test_later_us_sale_cannot_fund_an_earlier_london_buy():
    original = seed()
    original["cash"]["bank|USD"]["quantity"] = "0"
    packet = market("2026-01-05")
    packet["marks"]["AAA"]["close_at"] = "2026-01-05T21:00:00+00:00"
    packet["marks"]["BBB"]["close_at"] = "2026-01-05T16:30:00+00:00"
    raw = receipt()
    raw["sheet"]["lines"][1]["shares"] = 20
    applied, failures, fills = apply_sheet(new_book(original), raw, packet)
    assert applied is None and not fills
    assert any("cash insufficient" in reason for reason in failures)


def test_repriced_order_cannot_spend_the_deliberate_cash_reserve():
    raw = receipt(lines=[{"symbol": "BBB", "action": "BUY", "shares": 40, "instrument_type": "stock"}])
    raw["sheet"]["funding"] = {"reserve_usd": 500}
    applied, failures, _ = apply_sheet(new_book(seed()), raw, market("2026-01-05"))
    assert applied is None and any("cash reserve" in reason for reason in failures)


def test_zero_former_holdings_do_not_accrue_dividend_tax_unknowns():
    original = seed()
    original["positions"]["bank|AAA"]["quantity"] = "0"
    book = new_book(original)
    session_actions(book, market("2026-01-05", actions=[{
        "id": "div", "kind": "distribution", "instrument": "AAA", "amount": "1", "pay_date": None}]), net=True)
    assert not book["unknowns"] and not book["receivables"]

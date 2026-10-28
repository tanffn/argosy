"""Pure deterministic paper books. Judgment remains with the authoring fleet.

All values are native-currency Decimals until NAV translation. No snapshot
rebasing, inferred deposits, synthetic fills, guessed taxes, or silent resizing.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from argosy.services.after_tax import SaleTaxPolicy, TaxLotInput, calculate_after_tax_sale
from argosy.services.shadow_calendar import close_at, next_session
from argosy.services.shadow_calendar import sessions as calendar_sessions

VERSION = "whole-liquid-book-v1"
BOOKS = ("unchanged", "argosy", "spy")


class PaperEconomics(BaseModel):
    fee_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    basis_contract: str | None = None
    tax_policy: SaleTaxPolicy | None = None


def money(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite financial input")
    return result


def aware(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def currency(value):
    return "ILS" if value.upper() == "NIS" else value.upper()


def position_key(account, instrument):
    return account.strip().lower() + "|" + instrument


def new_book(seed):
    return {"positions": deepcopy(seed["positions"]), "cash": deepcopy(seed["cash"]),
            "cash_cutoffs": {}, "unresolved_inventory": [],
            "receivables": deepcopy(seed.get("opening_receivables", [])), "applied_actions": [], "fees_usd": "0", "taxes_usd": "0", "turnover_usd": "0", "unknowns": []}


def mark(book, market, day):
    """Missing components stay in the denominator; a partial sum is NOT NAV."""
    priced = Decimal(0)
    missing, stale = [], []
    for key, position in book["positions"].items():
        if position["instrument"] in book.get("unresolved_inventory", []):
            missing.append(key + ":unreconciled_corporate_action_history")
            continue
        if position.get("quantity") is None:
            missing.append(key + ":quantity_unknown")
            continue
        if money(position["quantity"]) == 0:
            continue
        if position["instrument"] in market.get("unresolved_actions", []):
            missing.append(key + ":corporate_action_coverage")
            continue
        price = market.get("marks", {}).get(position["instrument"])
        fx = market.get("fx", {}).get(price["currency"]) if price else None
        if not price or fx is None or money(price["price"]) <= 0 or money(fx) <= 0:
            missing.append(key)
            continue
        if price.get("as_of") != day:
            stale.append({"component": key, "as_of": price.get("as_of")})
        if price.get("as_of", "9999") > day:
            missing.append(key + ":future_quote")
            continue
        if not price.get("eligible", False):
            missing.append(key)
            continue
        priced += money(position["quantity"]) * money(price["price"]) * money(fx)
    cash_usd = Decimal(0)
    for key, cash in book["cash"].items():
        fx = market.get("fx", {}).get(cash["currency"])
        if cash.get("quantity") is None or fx is None:
            missing.append("cash:" + key)
        else:
            value = money(cash["quantity"]) * money(fx)
            cash_usd += value
            priced += value
    for receipt in book["receivables"]:
        fx = market.get("fx", {}).get(receipt["currency"])
        if fx is None:
            missing.append("distribution:" + receipt["id"])
        else:
            priced += money(receipt["amount"]) * money(fx)
    for ccy, evidence in market.get("fx_evidence", {}).items():
        if evidence.get("as_of") != day:
            stale.append({"component": "FX:" + ccy, "as_of": evidence.get("as_of")})
    return {"nav_usd": str(priced) if not missing else None,
            "covered_usd": str(priced), "cash_usd": str(cash_usd),
            "missing": missing, "stale_marks": stale, "coverage_complete": not missing}


def session_actions(book, market, *, net=False, dividend_tax=None):
    """Splits before trades; dividends belong to pre-ex-date holders.

    Unknown payment dates stay receivables, not spendable cash. Gross accrual
    needs no assumed payment date; net withholding/classification remain unknown.
    """
    for instrument in market.get("unresolved_actions", []):
        if any(position["instrument"] == instrument and
               (position.get("quantity") is None or money(position["quantity"]) != 0)
               for position in book["positions"].values()):
            if instrument not in book.setdefault("unresolved_inventory", []):
                book["unresolved_inventory"].append(instrument)
    for action in market.get("actions", []):
        if action["id"] in book["applied_actions"]:
            continue
        for key, position in list(book["positions"].items()):
            if position["instrument"] != action["instrument"]:
                continue
            if position.get("quantity") is None:
                book["unknowns"].append("Corporate action with unknown starting quantity: " + key)
                continue
            if money(position["quantity"]) == 0:
                continue
            if action["kind"] == "split":
                ratio = money(action["ratio"])
                if ratio <= 0:
                    raise ValueError("Invalid split ratio")
                position["quantity"] = str(money(position["quantity"]) * ratio)
                for lot in position.get("lots", []):
                    lot["quantity"] = str(money(lot["quantity"]) * ratio)
            elif action["kind"] == "distribution":
                ccy = market["marks"][position["instrument"]]["currency"]
                amount = money(position["quantity"]) * money(action["amount"])
                if net:
                    if dividend_tax is None or action.get("classification") != "ordinary_cash":
                        book["unknowns"].append("Distribution tax/classification unresolved: " + action["id"])
                    else:
                        tax = amount * money(dividend_tax)
                        book["taxes_usd"] = str(money(book["taxes_usd"]) + tax * money(market["fx"][ccy]))
                        amount -= tax
                book["receivables"].append({"id": action["id"] + "|" + key, "account": position["account"],
                    "currency": ccy, "amount": str(amount), "pay_date": action.get("pay_date")})
        book["applied_actions"].append(action["id"])
    remaining = []
    for receipt in book["receivables"]:
        if receipt["pay_date"] and receipt["pay_date"] <= market["date"]:
            key = position_key(receipt["account"], receipt["currency"])
            bucket = book["cash"].setdefault(key, {"account": receipt["account"], "currency": receipt["currency"], "quantity": "0"})
            if bucket["quantity"] is None:
                book["unknowns"].append("Unknown cash balance: " + key)
            else:
                bucket["quantity"] = str(money(bucket["quantity"]) + money(receipt["amount"]))
        else:
            remaining.append(receipt)
    book["receivables"] = remaining


def apply_sheet(book, receipt, market, *, net=False, inflows=()):
    """Atomic same-session exact-quantity execution, including sell-funded buys."""
    trial = deepcopy(book)
    failures, fills = [], []
    if net and trial["unknowns"]:
        return None, ["Prior unresolved tax/balance economics prevent funded net execution"], []
    if not receipt.get("validation", {}).get("valid"):
        return None, ["Invalid authored order sheet"], []
    lines = receipt["sheet"]["lines"]
    def order_key(line):
        quote = market.get("marks", {}).get(receipt.get("instruments", {}).get(line["symbol"]), {})
        cutoff = quote.get("close_at") or close_at(market["date"]).isoformat()
        return (aware(cutoff), line["action"] not in ("SELL", "TRIM"))
    for line in sorted(lines, key=order_key):
        symbol = line["symbol"]
        instrument = receipt.get("instruments", {}).get(symbol)
        account = receipt.get("accounts", {}).get(symbol)
        quote = market.get("marks", {}).get(instrument)
        if not account or not instrument or not quote or not quote.get("eligible"):
            failures.append(symbol + ": unresolved account/instrument/price")
            continue
        if instrument in market.get("unresolved_actions", []) or instrument in trial.get("unresolved_inventory", []):
            failures.append(symbol + ": unreconciled corporate-action history prevents execution")
            continue
        if line.get("instrument_type") == "option":
            failures.append(symbol + ": option lifecycle not supported")
            continue
        if quote["as_of"] != market["date"]:
            failures.append(symbol + ": cannot execute at a carried NAV/close")
            continue
        if aware(quote.get("close_at") or close_at(market["date"]).isoformat()) > aware(market.get("cutoff_at") or close_at(market["date"]).isoformat()):
            failures.append(symbol + ": quote close is after the common valuation cutoff")
            continue
        ccy = quote["currency"]
        fx = market["fx"].get(ccy)
        if fx is None:
            failures.append(symbol + ": missing dated FX")
            continue
        key = position_key(account, instrument)
        if key in trial["positions"] and trial["positions"][key].get("quantity") is None:
            failures.append(symbol + ": paper inventory insufficiently known")
            continue
        cash_key = position_key(account, ccy)
        trade_at = aware(quote.get("close_at") or close_at(market["date"]).isoformat())
        prior_cutoff = trial.get("cash_cutoffs", {}).get(cash_key)
        if prior_cutoff and aware(prior_cutoff) > trade_at:
            failures.append(symbol + ": cross-sheet chronology unresolved; later cash/inventory cannot fund an earlier trade")
            continue
        bucket = trial["cash"].setdefault(cash_key, {"account": account, "currency": ccy, "quantity": "0"})
        if bucket["quantity"] is None:
            failures.append(symbol + ": native cash balance unknown")
            continue
        quantity, price = money(line["shares"]), money(quote["price"])
        if quantity <= 0 or price <= 0 or money(fx) <= 0:
            failures.append(symbol + ": nonpositive execution quantity/price/FX")
            continue
        basis_date = str(line.get("evidence", {}).get("price_as_of") or receipt["published_at"])[:10]
        if any(action["instrument"] == instrument and action["kind"] == "split"
               and basis_date < action.get("ex_date", market["date"]) <= market["date"]
               for action in market.get("actions", [])):
            failures.append(symbol + ": split changed authored share units; a revised sheet is required")
            continue
        notional = quantity * price
        policy = {}
        if net:
            try:
                policy = PaperEconomics.model_validate((receipt.get("economics") or {}).get(symbol, {})).model_dump(mode="json")
            except (ValueError, TypeError, AttributeError) as exc:
                failures.append(symbol + ": invalid cost/tax evidence: " + str(exc)[:150])
                continue
        fee_usd = policy.get("fee_usd")
        if net and fee_usd is None:
            failures.append(symbol + ": trading costs unknown")
            continue
        if net and ccy != "USD":
            failures.append(symbol + ": native commission/FX settlement evidence unresolved")
            continue
        fee = money(fee_usd or 0) / money(fx) if net else Decimal(0)
        tax_usd = Decimal(0)
        consumed = []
        if line["action"] in ("SELL", "TRIM"):
            position = trial["positions"].get(key)
            if not position or position.get("quantity") is None or money(position["quantity"]) < quantity:
                failures.append(symbol + ": paper inventory insufficient")
                continue
            if net:
                tax_policy = policy.get("tax_policy")
                if (ccy != "USD" or not tax_policy or policy.get("basis_contract") != "verified_usd_hifo"
                        or (line.get("tax") or {}).get("calculation_basis", "capital_gain") != "capital_gain"):
                    failures.append(symbol + ": compatible tax policy/basis unresolved (including Section 102)")
                    continue
                try:
                    lots = [TaxLotInput(**lot) for lot in position.get("lots", []) if money(lot["quantity"]) > 0]
                    tax = calculate_after_tax_sale(gross_proceeds_usd=float(notional), current_price_usd=float(price),
                        lots=lots, policy=SaleTaxPolicy(**tax_policy), as_of=aware(market["date"] + "T21:00:00+00:00"),
                        friction_usd=float(fee))
                except (ValueError, TypeError, KeyError) as exc:
                    failures.append(symbol + ": unsupported tax/lot evidence: " + str(exc)[:150])
                    continue
                if not tax.eligible:
                    failures.extend(symbol + ": " + reason for reason in tax.failures)
                    continue
                tax_usd = money(tax.tax.estimated_tax_usd)
                remaining = quantity
                for lot in sorted(position["lots"], key=lambda row: money(row["cost_basis_usd"]) / money(row["quantity"]), reverse=True):
                    if remaining <= 0:
                        break
                    take = min(remaining, money(lot["quantity"]))
                    basis = money(lot["cost_basis_usd"]) * take / money(lot["quantity"])
                    consumed.append({"lot_id": lot["lot_id"], "quantity": str(take), "cost_basis_usd": str(basis)})
                    lot["quantity"] = str(money(lot["quantity"]) - take)
                    lot["cost_basis_usd"] = str(money(lot["cost_basis_usd"]) - basis)
                    remaining -= take
                position["lots"] = [lot for lot in position["lots"] if money(lot["quantity"]) > 0]
            position["quantity"] = str(money(position["quantity"]) - quantity)
            bucket["quantity"] = str(money(bucket["quantity"]) + notional - tax_usd / money(fx) - fee)
        else:
            required = notional + fee
            unavailable = sum((money(flow["amount"]) for flow in inflows
                if flow["account"] == account and flow["currency"] == ccy
                and aware(flow.get("effective_at") or close_at(market["date"]).isoformat()) > trade_at), Decimal(0))
            if money(bucket["quantity"]) - unavailable < required:
                failures.append(symbol + ": paper cash insufficient; no inferred deposit/FX transfer or resize")
                continue
            bucket["quantity"] = str(money(bucket["quantity"]) - required)
            position = trial["positions"].setdefault(key, {"instrument": instrument, "account": account,
                "currency": ccy, "quantity": "0", "lots": []})
            position["quantity"] = str(money(position["quantity"]) + quantity)
            position["lots"].append({"lot_id": receipt["receipt_id"] + "|" + symbol,
                "quantity": str(quantity), "cost_basis_usd": str((notional + fee) * money(fx))})
        trial["fees_usd"] = str(money(trial["fees_usd"]) + fee * money(fx))
        trial.setdefault("cash_cutoffs", {})[cash_key] = trade_at.isoformat()
        trial["taxes_usd"] = str(money(trial["taxes_usd"]) + tax_usd)
        trial["turnover_usd"] = str(money(trial["turnover_usd"]) + notional * money(fx))
        fills.append({"symbol": symbol, "action": line["action"], "account": account, "quantity": str(quantity),
            "price": str(price), "currency": ccy, "source": quote["source"], "tax_usd": str(tax_usd),
            "fee_usd": str(fee * money(fx)), "consumed_lots": consumed, "paper_only": True})
    if any(bucket["quantity"] is not None and money(bucket["quantity"]) < 0 for bucket in trial["cash"].values()):
        failures.append("Negative cash would require unmodeled borrowing")
    reserve = money(receipt["sheet"].get("funding", {}).get("reserve_usd", 0))
    if reserve > 0:
        funding_accounts = {receipt["accounts"].get(line["symbol"]) for line in lines if line["action"] in ("BUY", "ADD")}
        if len(funding_accounts) != 1:
            failures.append("Cash reserve custody unresolved")
        else:
            account = next(iter(funding_accounts))
            balances = [bucket for bucket in trial["cash"].values() if bucket["account"] == account]
            if any(bucket["quantity"] is None or market["fx"].get(bucket["currency"]) is None for bucket in balances):
                failures.append("Cash reserve value unresolved")
            elif sum((money(bucket["quantity"]) * money(market["fx"][bucket["currency"]]) for bucket in balances), Decimal(0)) < reserve:
                failures.append("Exact authored quantities would spend the conserved cash reserve")
    return (None, failures, []) if failures else (trial, [], fills)


def credit_flow(book, flow):
    key = position_key(flow["account"], flow["currency"])
    bucket = book["cash"].setdefault(key, {"account": flow["account"], "currency": flow["currency"], "quantity": "0"})
    if bucket["quantity"] is None:
        book["unknowns"].append("External flow added to unresolved opening cash: " + key)
    else:
        bucket["quantity"] = str(money(bucket["quantity"]) + money(flow["amount"]))


def replay(seed, sessions, receipts=(), flows=()):
    """Replay all three paths and independent gross/net books from frozen inputs."""
    paths = {mode: {name: new_book(seed) for name in BOOKS} for mode in ("gross", "net")}
    history, applications, initial, seen_intents, seen_executions = [], [], {}, set(), set()
    seen_sources = set()
    first_publication = {}
    first_execution_publication = {}
    first_source_publication = {}
    for receipt in receipts:
        if not receipt["sheet"].get("lines") or receipt.get("withdrawn"):
            continue
        first_publication[receipt["intent_id"]] = min(aware(receipt["published_at"]),
            first_publication.get(receipt["intent_id"], aware(receipt["published_at"])))
        if receipt.get("execution_key"):
            key = receipt["execution_key"]
            first_execution_publication[key] = min(aware(receipt["published_at"]), first_execution_publication.get(key, aware(receipt["published_at"])))
        for source in receipt.get("source_intents", []):
            first_source_publication[source] = min(aware(receipt["published_at"]),
                first_source_publication.get(source, aware(receipt["published_at"])))
    previous_nav, growth, broken_intervals = {}, {}, set()
    first_date = next_session(seed["activation_date"])
    packets = {row["date"]: row for row in sessions}
    scheduled = calendar_sessions(first_date, max(packets, default=seed["activation_date"]))
    sessions = [packets.get(day, {"date": day, "marks": {}, "fx": {"USD": "1"}, "actions": []}) for day in scheduled]
    net_failed, strategy_failed = set(), set()
    receipts = sorted(receipts, key=lambda row: (row["published_at"], row.get("publication_revision", 0), row["receipt_id"]))
    receipt_order = {row["receipt_id"]: index for index, row in enumerate(receipts)}
    processed = set()
    processed_flows = set()
    for market in sorted(sessions, key=lambda row: row["date"]):
        day = market["date"]
        if day <= seed["activation_date"]:
            continue
        if not initial:
            if day != first_date:
                continue  # Never move an incomplete baseline to a favorable later day.
            for index, flow in enumerate(flows):
                if flow["date"] <= day:
                    for books in paths.values():
                        for name in ("unchanged", "argosy"):
                            credit_flow(books[name], flow)
                    processed_flows.add(index)
            for mode, books in paths.items():
                for name, book in books.items():
                    if name != "spy":
                        session_actions(book, market, net=mode == "net", dividend_tax=seed.get("dividend_tax_rate"))
            baseline = mark(paths["gross"]["unchanged"], market, day)
            spy = market.get("marks", {}).get("SPY")
            if not baseline["coverage_complete"] or not spy or not spy.get("eligible") or spy["as_of"] != day:
                history.append({"date": day, "status": "baseline_incomplete", "baseline": baseline})
                continue
            capital = money(baseline["nav_usd"])
            if capital <= 0:
                raise ValueError("A proportional benchmark requires positive starting capital")
            for mode in paths:
                mode_capital = money(mark(paths[mode]["unchanged"], market, day)["nav_usd"])
                paths[mode]["spy"] = new_book({"positions": {"benchmark|SPY": {
                    "instrument": "SPY", "account": "benchmark", "currency": "USD",
                    "quantity": str(mode_capital / money(spy["price"])), "lots": []}}, "cash": {}})
                paths[mode]["spy"]["unknowns"] = list(paths[mode]["unchanged"]["unknowns"])
                for name in BOOKS:
                    initial[(mode, name)] = mode_capital
                    previous_nav[(mode, name)] = mode_capital
                    growth[(mode, name)] = Decimal(1)
            baseline_date = day
        else:
            for mode, books in paths.items():
                for _name, book in books.items():
                    session_actions(book, market, net=mode == "net", dividend_tax=seed.get("dividend_tax_rate"))
        flow_usd = Decimal(0)
        day_inflows = []
        for index, flow in enumerate(flows):
            if index in processed_flows or flow["date"] != day:
                continue
            processed_flows.add(index)
            day_inflows.append(flow)
            # Exogenous flows require a dated native cash receipt, never sheet.new_cash.
            amount, ccy = money(flow["amount"]), flow["currency"]
            if amount < 0:
                raise ValueError("Withdrawals need explicit per-book liquidation instructions")
            fx = market["fx"].get(ccy)
            usd = amount * money(fx) if fx is not None else None
            flow_usd = flow_usd + usd if flow_usd is not None and usd is not None else None
            for _mode, books in paths.items():
                for name, book in books.items():
                    if name == "spy":
                        quote = market.get("marks", {}).get("SPY")
                        if usd is not None and quote and quote.get("eligible") and quote["as_of"] == day:
                            position = book["positions"]["benchmark|SPY"]
                            position["quantity"] = str(money(position["quantity"]) + usd / money(quote["price"]))
                        else:
                            credit_flow(book, {**flow, "account": "benchmark"})
                            book["unknowns"].append("External-flow benchmark allocation missing dated SPY/FX evidence")
                    else:
                        credit_flow(book, flow)
        for receipt in receipts:
            rid = receipt["receipt_id"]
            published_date = receipt.get("publication_date", receipt["published_at"][:10])
            if rid in processed or day <= published_date:
                continue
            cutoff = close_at(baseline_date)
            if (first_publication.get(receipt["intent_id"], aware(receipt["published_at"])) <= cutoff
                    or first_execution_publication.get(receipt.get("execution_key"), aware(receipt["published_at"])) <= cutoff):
                applications.append({"receipt_id": rid, "status": "pre_baseline", "date": day})
                processed.add(rid)
                continue
            intent = receipt["intent_id"]
            execution_key = receipt.get("execution_key")
            if intent in seen_intents or execution_key and execution_key in seen_executions:
                applications.append({"receipt_id": rid, "status": "refresh_not_an_additional_trade", "date": day})
                processed.add(rid)
                continue
            # The latest visible revision of a pending intent replaces its predecessor.
            later = [r for r in receipts if (r["intent_id"] == intent or
                     r.get("publication_id") == receipt.get("publication_id") and r.get("publication_id") is not None)
                     and receipt_order[r["receipt_id"]] > receipt_order[rid]
                     and r.get("publication_date", r["published_at"][:10]) < day]
            if later:
                applications.append({"receipt_id": rid, "status": "superseded_before_execution", "date": day})
                processed.add(rid)
                continue
            processed.add(rid)
            if receipt.get("withdrawn") or (not receipt["sheet"].get("lines") and receipt.get("validation", {}).get("valid")):
                applications.append({"receipt_id": rid, "status": "withdrawn" if receipt.get("withdrawn") else "no_action", "date": day})
                continue
            seen_intents.add(intent)
            if execution_key:
                seen_executions.add(execution_key)
            overlapping = bool(seen_sources.intersection(receipt.get("source_intents", []))) or any(
                first_source_publication.get(source, aware(receipt["published_at"])) <= cutoff
                for source in receipt.get("source_intents", []))
            explicit_addition = (receipt.get("envelope") or {}).get("additional_intent_id")
            for mode in paths:
                if overlapping and not explicit_addition:
                    book, failures, fills = None, ["Overlapping source intents require explicit prospective additional orders"], []
                else:
                    book, failures, fills = apply_sheet(paths[mode]["argosy"], receipt, market, net=mode == "net", inflows=day_inflows)
                if failures:
                    (net_failed if mode == "net" else strategy_failed).add(rid)
                else:
                    paths[mode]["argosy"] = book
                applications.append({"receipt_id": rid, "mode": mode, "date": day,
                    "status": "blocked" if failures else "applied", "failures": failures, "fills": fills})
            seen_sources.update(receipt.get("source_intents", []))
        marks = {}
        for mode, books in paths.items():
            marks[mode] = {}
            for name, book in books.items():
                value = mark(book, market, day)
                problems = list(book["unknowns"])
                if flow_usd is None:
                    problems.append("External flow USD valuation unavailable")
                if name == "argosy" and (net_failed if mode == "net" else strategy_failed):
                    problems.append("Some authored recommendations could not be replayed")
                nav = money(value["nav_usd"]) if value["nav_usd"] is not None else None
                identity = (mode, name)
                if nav is None or problems:
                    broken_intervals.add(identity)
                elif identity not in broken_intervals:
                    growth[identity] *= (nav - flow_usd) / previous_nav[identity]
                    previous_nav[identity] = nav
                if identity in broken_intervals and not problems:
                    problems.append("Incomplete prior valuation interval; repair historical evidence before comparing returns")
                value.update(return_pct=str((growth[identity] - 1) * 100)
                    if nav is not None and not problems and identity not in broken_intervals else None,
                    unknowns=problems, taxes_usd=book["taxes_usd"], fees_usd=book["fees_usd"],
                    turnover_usd=book["turnover_usd"], after_tax_nav_usd=str(nav) if mode == "net" and nav is not None and not problems else None)
                marks[mode][name] = value
        history.append({"date": day, "status": "valued", "books": marks})
    return {"version": VERSION, "baseline_date": baseline_date if initial else None,
        "starting_nav_usd": str(capital) if initial else None,
        "status": ("partial" if strategy_failed or any(book["return_pct"] is None for book in history[-1]["books"]["gross"].values())
                   else "collecting") if initial else "baseline_incomplete" if history else "awaiting_baseline", "history": history,
        "applications": applications, "books": paths,
        "pending_receipts": [r["receipt_id"] for r in receipts if r["receipt_id"] not in processed]}

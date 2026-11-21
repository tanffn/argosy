"""Versioned native-price/FX evidence, separate from prediction total-return bars.

Yahoo Close is split-adjusted, not dividend-adjusted. Restore as-traded share
units using all subsequent splits visible at retrieval; never add dividends to
Adj Close returns. Frozen normalized daily evidence is replayed offline.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from argosy.services.shadow_accounting import money
from argosy.services.shadow_calendar import calendar, close_at, listing_calendar
from argosy.services.shadow_calendar import sessions as calendar_sessions

CONTRACT = "split-restored-close-accrual-v1"


def completed_date(now):
    local = now.astimezone(ZoneInfo("America/New_York"))
    return local.date() if local.time() >= time(16, 30) else local.date() - timedelta(days=1)


def _observed_number(value):
    try:
        return money(value)
    except (ValueError, InvalidOperation):
        return None


def normalize_history(frame, *, instrument, quote_currency, source, calendar_name="XNYS",
                      latest_missing_session=None):
    required = {"Close"} if calendar_name == "24/5" else {"Close", "Dividends", "Stock Splits"}
    if not required <= set(frame.columns):
        raise ValueError("Missing explicit price/corporate-action columns; absence is not zero events")
    scale = Decimal("0.01") if quote_currency in ("GBp", "GBX", "ILA") else Decimal(1)
    ccy = {"GBp": "GBP", "GBX": "GBP", "ILA": "ILS"}.get(quote_currency, quote_currency)
    result = {}
    # Yahoo adjusts every older Close for later splits. An unknown split ratio
    # invalidates older units, but leaves later observations independent.
    factor = Decimal(1)
    for stamp, row in frame.sort_index(ascending=False).iterrows():
        day = stamp.date()
        if latest_missing_session is not None and day < latest_missing_session:
            factor = None
        observed_close = _observed_number(row["Close"])
        issues = []
        price = None
        if observed_close is None or observed_close <= 0:
            issues.append("Missing or nonpositive market close")
        elif factor is None:
            issues.append("Price units unresolved due to a missing later split ratio")
        else:
            price = observed_close * factor * scale
        actions = []
        action_complete = True
        split = _observed_number(row.get("Stock Splits", 0))
        if split is None or split < 0:
            action_complete = False
            split = None
            issues.append("Missing or invalid split ratio")
        if split:
            actions.append({"id": f"{instrument}|split|{day}", "instrument": instrument,
                            "kind": "split", "ratio": str(split)})
        for column, kind in (("Dividends", "distribution"), ("Capital Gains", "capital_distribution")):
            if column not in frame.columns:
                continue
            amount = _observed_number(row[column])
            if amount is None or (amount and factor is None):
                action_complete = False
                issues.append(f"Missing or unresolved {column} amount")
            elif amount:
                actions.append({"id": f"{instrument}|{kind}|{day}", "instrument": instrument,
                    "kind": "distribution", "amount": str(amount * factor * scale),
                    "classification": f"unverified_{kind}", "pay_date": None})
        record = {"price": str(price) if price is not None else None, "currency": ccy, "as_of": day.isoformat(),
            "source": source, "eligible": price is not None,
            "reported_close": str(observed_close) if observed_close is not None else None,
            "split_restoration_factor": str(factor) if factor is not None else None,
            "quote_scale": str(scale), "contract": CONTRACT,
            "calendar": calendar_name, "close_at": close_at(day.isoformat(), calendar_name).isoformat()
                if calendar_name != "24/5" else (datetime.combine(day + timedelta(days=1), time(0), tzinfo=UTC)).isoformat(),
            "action_history_complete": action_complete, "actions": actions}
        if issues:
            record["data_issues"] = issues
        result[day.isoformat()] = record
        for action in actions:
            action["ex_date"] = day.isoformat()
        if split is None:
            factor = None
        elif split and factor is not None:
            factor *= split
    return result


def compatible_prior_mark(mark, observation):
    """An unavailable close may reuse an independently observed price for the same session and units."""
    if not mark.get("eligible") or any(mark.get(field) != observation.get(field)
                                       for field in ("as_of", "currency", "contract")):
        return False
    for field in ("quote_scale", "split_restoration_factor"):
        unit = _observed_number(observation.get(field))
        if unit is None or unit <= 0 or unit != _observed_number(mark.get(field)):
            return False
    return True


def fetch_listing(identity, start, now):
    import yfinance as yf
    provider = identity.get("provider")
    if not provider or provider.startswith("TASE:"):
        return {}, "Unresolved/non-Yahoo listing"
    ticker = yf.Ticker(provider)
    try:
        frame = ticker.history(start=(start - timedelta(days=10)).isoformat(),
            end=(now.date() + timedelta(days=1)).isoformat(), auto_adjust=False,
            back_adjust=False, actions=True, repair=False, timeout=20, raise_errors=True)
        if frame.empty:
            return {}, "No historical market bars"
        metadata = ticker.history_metadata or {}
        ccy = metadata.get("currency")
        if not ccy:
            return {}, "Quote denomination unavailable"
        calendar_name = "24/5" if provider.endswith("=X") else listing_calendar(provider)
        latest_missing_session = None
        if calendar_name != "24/5":
            observed = {stamp.date() for stamp in frame.index}
            expected = {stamp.date() for stamp in calendar(calendar_name).sessions_in_range(
                min(observed).isoformat(), now.date().isoformat()) if close_at(stamp.date().isoformat(), calendar_name) < now}
            latest_missing_session = max(expected - observed, default=None)
        normalized = normalize_history(frame, instrument=identity["instrument"], quote_currency=ccy,
            calendar_name=calendar_name, source=f"https://finance.yahoo.com/quote/{provider}/history/",
            latest_missing_session=latest_missing_session)
        return normalized, None
    except Exception as exc:
        return {}, str(exc)[:350]


def collect(seed, receipts, *, now, max_days=31, existing_days=(), fund_observations=None, retry_days=()):
    """No DB transaction spans external requests; omissions are explicit."""
    start = date.fromisoformat(seed["activation_date"]) + timedelta(days=1)
    last = completed_date(now)
    if last < start:
        return [], {"status": "awaiting_completed_session", "remaining_sessions": 0}
    days = [day for day in calendar_sessions(start.isoformat(), last.isoformat())
            if close_at(day) < now]
    if not days:
        return [], {"status": "awaiting_completed_session", "remaining_sessions": 0}
    identities = dict(seed["identities"])
    for receipt in receipts:
        identities.update(receipt.get("listing_identities", {}))
        for line in receipt["sheet"].get("lines", []):
            symbol = line["symbol"]
            instrument = receipt["instruments"].get(symbol)
            if instrument and instrument not in identities:
                # A new stock's exact ticker is explicit. A new foreign ETF
                # needs an evidenced provider listing, never a bare-US fallback.
                import re
                source = line.get("evidence", {}).get("price_source", "")
                listing = re.search(r"\b" + re.escape(symbol) + r"\.(L|SW|AS|MI|DE)\b", source)
                country = line.get("evidence", {}).get("incorporation_country", "").upper()
                foreign_fund = line.get("instrument_type") == "etf" and country not in ("US", "USA", "UNITED STATES")
                identities[instrument] = {"instrument": instrument, "symbol": symbol,
                    "provider": listing[0] if listing else None if foreign_fund else symbol.replace("/", "-")}
    def one(pair):
        key, identity = pair
        return key, fetch_listing(identity, start, now)
    with ThreadPoolExecutor(max_workers=6) as pool:
        downloaded = dict(pool.map(one, [(key, ref) for key, ref in identities.items() if not key.startswith("TASE:")]))
    wanted = sorted(set([day for day in days if day not in set(existing_days)][:max_days]
                        + [day for day in days[-7:] if day in set(existing_days)]
                        + [day for day in retry_days if day in days]))
    errors = {key: failure for key, (_, failure) in downloaded.items() if failure}
    fund_marks = {key: list(values) for key, values in (fund_observations or {}).items()}
    for key, ref in identities.items():
        if not key.startswith("TASE:"):
            continue
        try:
            from argosy.adapters.data.israeli_funds import get_fund_quote
            q = get_fund_quote(key.split(":", 1)[1], today=now.date())
            fund_marks.setdefault(key, []).append({"price": str(q.price_ils), "currency": "ILS", "as_of": q.as_of.isoformat(),
                "source": q.source_url, "source_hash": q.sha256, "eligible": True, "contract": "official-fund-nav-ils-v1"})
        except Exception as exc:
            errors[key] = str(exc)[:350]
            # Frozen official NAV is usable only within the declared carry bound.
            if (ref.get("recorded_price") and ref.get("recorded_mark_date") and ref.get("recorded_price_hash")
                    and str(ref.get("recorded_price_source") or "").startswith("https://")):
                fund_marks.setdefault(key, []).append({"price": str(ref["recorded_price"]), "currency": "ILS",
                    "as_of": ref["recorded_mark_date"], "source": ref["recorded_price_source"],
                    "eligible": True, "contract": "recorded-official-nav-ils-v1"})
    ccys = {bucket["currency"] for bucket in seed["cash"].values()}
    ccys.update(quote["currency"] for bars, _ in downloaded.values() for quote in bars.values())
    ccys.update(ref["currency"] for values in fund_marks.values() for ref in values)
    fx_histories = {}
    for ccy in sorted(ccys - {"USD", "UNKNOWN", ""}):
        values, failure = fetch_listing({"instrument": "FX:" + ccy, "provider": ccy + "USD=X"}, start, now)
        if failure:
            errors["FX:" + ccy] = failure
        fx_histories[ccy] = values
    sessions = []
    session_errors = {}
    for day in wanted:
        marks, actions, fx, fx_evidence = {}, [], {"USD": "1"}, {}
        covered, unresolved = [], []
        day_errors = dict(errors)
        unpriced_marks = {}
        previous = max([at for at in days if at < day], default=seed["activation_date"])
        for key, (bars, failure) in downloaded.items():
            schedule = calendar(listing_calendar(identities[key].get("provider")))
            expected = {stamp.date().isoformat() for stamp in schedule.sessions_in_range(previous, day)
                        if previous < stamp.date().isoformat() <= day}
            complete = not failure and all(
                bars.get(at, {}).get("action_history_complete") is True for at in expected)
            (covered if complete else unresolved).append(key)
            for at in sorted(expected):
                bar = bars.get(at, {})
                if not bar and not failure:
                    day_errors[key] = f"{at}: Missing exchange-session bar"
                    session_errors[key] = day_errors[key]
                elif bar.get("data_issues"):
                    day_errors[key] = f"{at}: {', '.join(bar['data_issues'])}"
                    session_errors[key] = day_errors[key]
                if complete:
                    actions.extend(bar["actions"])
            eligible = [at for at in bars if at <= day and (date.fromisoformat(day) - date.fromisoformat(at)).days <= 4]
            quote = bars[max(eligible)] if eligible else None
            if quote:
                if quote["as_of"] != day:
                    provider = identities[key].get("provider")
                    if calendar(listing_calendar(provider)).is_session(day):
                        quote = None  # Missing expected open-session quote is not a holiday.
            if quote and quote["eligible"]:
                marks[key] = {k: v for k, v in quote.items() if k != "actions"}
            elif quote:
                unpriced_marks[key] = {field: quote[field] for field in
                    ("as_of", "currency", "contract", "quote_scale", "split_restoration_factor")}
        for key, values in fund_marks.items():
            eligible = [quote for quote in values if 0 <= (date.fromisoformat(day) - date.fromisoformat(quote["as_of"])).days <= 7]
            if eligible:
                marks[key] = max(eligible, key=lambda quote: quote["as_of"])
        for ccy, bars in fx_histories.items():
            eligible = [at for at in bars if bars[at]["eligible"] and at < day
                        and (date.fromisoformat(day) - date.fromisoformat(at)).days <= 4]
            if eligible:
                evidence = bars[max(eligible)]
                fx[ccy] = evidence["price"]
                fx_evidence[ccy] = evidence
        sessions.append({"date": day, "marks": marks, "unpriced_marks": unpriced_marks, "actions": actions, "fx": fx,
                         "fx_evidence": fx_evidence, "contract": CONTRACT, "failures": day_errors,
                         "cutoff_at": close_at(day).isoformat(), "action_coverage": covered,
                         "unresolved_actions": unresolved})
    return sessions, {"status": "collected" if sessions else "up_to_date" if days else "market_data_unavailable",
                      "sessions": len(sessions), "remaining_sessions": len(set(days) - set(existing_days) - set(wanted)), "failures": {**errors, **session_errors}}

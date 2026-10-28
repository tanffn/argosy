"""Durable forward-only portfolio comparison; no real execution capabilities."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, inspect, select, text
from sqlalchemy.dialects.sqlite import insert

from argosy.services.shadow_accounting import VERSION, aware, currency, money, position_key, replay
from argosy.state.models import ActionProposal, Lot, PortfolioSnapshotRow
from argosy.state.shadow_models import ShadowEvent

NY = ZoneInfo("America/New_York")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def append_event(connection, *, user_id, cohort_id, event_key, kind, payload, effective_at, now=None):
    """DB-enforced exactly-once append. Never change a recorded financial fact."""
    encoded = canonical(payload)
    signature = hashlib.sha256(encoded.encode()).hexdigest()
    connection.execute(insert(ShadowEvent.__table__).values(user_id=user_id, cohort_id=cohort_id,
        event_key=event_key, kind=kind, effective_at=aware(effective_at), recorded_at=now or datetime.now(UTC),
        payload_json=encoded, content_hash=signature).on_conflict_do_nothing(
            index_elements=["user_id", "cohort_id", "event_key"]))
    row = connection.execute(select(ShadowEvent.__table__).where(ShadowEvent.user_id == user_id,
        ShadowEvent.cohort_id == cohort_id, ShadowEvent.event_key == event_key)).mappings().one()
    if row["content_hash"] != signature:
        raise ValueError("Immutable shadow event identity reused with different evidence")
    return row["id"]


def baseline_event(connection, user_id):
    if not inspect(connection).has_table("shadow_events"):
        return None
    return connection.execute(select(ShadowEvent.__table__).where(ShadowEvent.user_id == user_id,
        ShadowEvent.kind == "baseline", ShadowEvent.cohort_id == digest([user_id, VERSION]))
        .order_by(ShadowEvent.id.desc()).limit(1)).mappings().first()


def activate(session, *, user_id, now=None):
    now = aware(now or datetime.now(UTC))
    if not session.in_transaction():
        session.execute(text("BEGIN IMMEDIATE"))
    existing = baseline_event(session.connection(), user_id)
    if existing:
        return json.loads(existing["payload_json"])
    snapshot = session.scalar(select(PortfolioSnapshotRow).where(PortfolioSnapshotRow.user_id == user_id,
        PortfolioSnapshotRow.imported_at <= now).order_by(PortfolioSnapshotRow.imported_at.desc(), PortfolioSnapshotRow.id.desc()).limit(1))
    if snapshot is None:
        raise ValueError("No recorded portfolio snapshot for comparison")
    positions, cash, identities, excluded, warnings = {}, {}, {}, [], []
    original = json.loads(snapshot.positions_json)
    for item in original:
        symbol = str(item.get("symbol") or "").strip().upper()
        account = str(item.get("location") or "").strip().lower()
        ccy = currency(str(item.get("currency") or "UNKNOWN"))
        if symbol == "-" and "real estate" in str(item.get("asset_type") or "").lower():
            excluded.append({"name": item.get("details"), "reason": "Physical property is outside the traded liquid portfolio", "recorded_usd_k": item.get("usd_value_k")})
            continue
        if not symbol and str(item.get("asset_type") or "").lower() == "cash":
            key = position_key(account, ccy)
            native = item.get("current_value_local")
            if key in cash:
                prior = cash[key]["quantity"]
                cash[key]["quantity"] = str(money(prior) + money(native)) if prior is not None and native is not None else None
                cash[key].setdefault("source_rows", []).append(item)
            else:
                cash[key] = {"account": account, "currency": ccy, "quantity": str(native) if native is not None else None,
                             "observed_as_of": item.get("observed_as_of"), "source_rows": [item]}
            continue
        instrument = "TASE:" + str(item["security_id"]) if item.get("quote_market") == "TASE_FUND" else symbol
        provider = None
        if instrument.startswith("TASE:"):
            provider = instrument
        elif listing := re.search(r"\b(LN|SW|GY|NA|IM)$", str(item.get("details") or "").strip().upper()):
            provider = symbol + {"LN": ".L", "SW": ".SW", "GY": ".DE", "NA": ".AS", "IM": ".MI"}[listing[1]]
        else:
            # Exposure metadata is not a listing master. Foreign ETF identities
            # must not fall through to an unrelated bare US ticker.
            from argosy.services.instrument_reference import STRUCT_ETF, lookup
            reference = lookup(symbol)
            foreign = reference and reference.structure == STRUCT_ETF and reference.estate_safe
            if not foreign:
                provider = symbol.replace("/", "-")
        if provider and not instrument.startswith("TASE:") and "." in provider:
            instrument = provider + "@" + ccy
        identities[instrument] = {"instrument": instrument, "symbol": symbol, "provider": provider,
            "currency": ccy, "identity_evidence": item.get("identity_source") or item.get("details"),
            "recorded_mark_date": item.get("valued_as_of"), "recorded_price": item.get("current_price"),
            "recorded_price_source": item.get("price_source"), "recorded_price_hash": item.get("price_evidence_sha256")}
        key = position_key(account, instrument)
        if key in positions:
            raise ValueError("Duplicate account/security rows require explicit merge before activation")
        lots = session.scalars(select(Lot).where(Lot.user_id == user_id, Lot.ticker == symbol,
            Lot.account_id == account, Lot.imported_at <= now, Lot.quantity > 0).order_by(Lot.id)).all()
        positions[key] = {"instrument": instrument, "symbol": symbol, "account": account, "currency": ccy,
            "quantity": str(item["shares"]) if item.get("shares") is not None else None,
            "observed_as_of": item.get("observed_as_of"), "recorded_usd_k": item.get("usd_value_k"),
            "lots": [{"lot_id": str(lot.id), "quantity": str(lot.quantity), "cost_basis_usd": str(lot.cost_basis_usd)} for lot in lots]}
        if (str(item.get("asset_type") or "").lower() in ("option", "options", "future", "futures")
                or re.fullmatch(r"[A-Z]{1,6}\d{6}[CP]\d{8}", symbol)
                or item.get("shares") is not None and money(item["shares"]) < 0):
            positions[key]["recorded_quantity"] = positions[key]["quantity"]
            positions[key]["quantity"] = None
            warnings.append({"component": key, "reason": "Derivative/short units require explicit lifecycle accounting; not silently priced as stock shares"})
        if str(item.get("observed_as_of") or "")[:10] != now.date().isoformat():
            warnings.append({"component": key, "holding_observed_as_of": item.get("observed_as_of"), "reason": "Recorded units, not independently verified current custody"})
    identities["SPY"] = {"instrument": "SPY", "symbol": "SPY", "provider": "SPY", "currency": "USD", "identity_evidence": "Declared S&P 500 ETF benchmark"}
    liquid_rows = [item for item in original if not (item.get("symbol") == "-" and "real estate" in str(item.get("asset_type") or "").lower())]
    seed = {"version": VERSION, "cohort_id": digest([user_id, VERSION]), "user_id": user_id,
        "activation_at": now.isoformat(), "activation_date": now.astimezone(NY).date().isoformat(),
        "snapshot_id": snapshot.id, "snapshot_imported_at": aware(snapshot.imported_at).isoformat(),
        "snapshot_hash": digest(original), "positions": positions, "cash": cash,
        "opening_receivables": [],
        "identities": identities, "excluded": excluded, "warnings": warnings,
        "recorded_liquid_usd": str(sum((money(item["usd_value_k"]) * 1000 for item in liquid_rows), money(0)))
            if all(item.get("usd_value_k") is not None for item in liquid_rows) else None,
        "policy": {"entry": "First completed SPY session date strictly after publication NY date; common EOD",
            "baseline": "First completed SPY session strictly after activation NY date",
            "tax": "After modeled realized tax only; no hypothetical liquidation tax; unknowns are not zero",
            "distribution": "Accrue at ex-date; not spendable until evidenced payment date",
            "funding": "Same-session sale proceeds available only within exact native-currency account",
            "cash_interest": "Not modeled; no invented cash yield", "flows": "Explicit native receipts only; never snapshot deltas or sheet funding amounts"}}
    seed["policy"]["opening_entitlements"] = "Initial endowment is recorded securities and cash only. Unrecorded pre-activation receivables/liabilities are excluded, never added as subsequent gains."
    seed["policy"]["fx"] = "Latest completed daily FX close strictly before the valuation session; carry age retained, no future intraday FX."
    seed["policy"]["income_coverage"] = "Provider-reported distributions only; official Israeli fund observations are NAV-only. Unverified distribution taxes or payment dates remain unresolved. This is not reconciled custody total return."
    append_event(session.connection(), user_id=user_id, cohort_id=seed["cohort_id"], event_key="baseline",
                 kind="baseline", payload=seed, effective_at=now, now=now)
    for publication in session.scalars(select(ActionProposal).where(ActionProposal.user_id == user_id)):
        _capture(session.connection(), publication, now=now, preexisting=True)
    return seed


def _capture(connection, proposal, *, now=None, preexisting=False):
    base = baseline_event(connection, proposal.user_id)
    if base is None:
        return
    seed = json.loads(base["payload_json"])
    now = aware(now or datetime.now(UTC))
    source_time = aware(proposal.surfaced_at)
    # A pre-existing row can be refreshed in place without changing surfaced_at.
    # Capture changed contents at NOW; predecessor/source history prevents a
    # stale recommendation from becoming a backdated or duplicate paper trade.
    try:
        envelope = json.loads(proposal.suggested_payload)
    except (TypeError, ValueError):
        return
    raw = envelope.get("order_sheet") if isinstance(envelope, dict) else None
    if not isinstance(raw, dict):
        return
    from argosy.services.order_sheet import OrderSheet, validate_order_sheet
    from argosy.services.order_sheet_materializer import order_sheet_fingerprint
    validation = {"valid": False, "failures": []}
    try:
        sheet = OrderSheet.model_validate(raw)
        validation = validate_order_sheet(sheet).model_dump(mode="json")
        if sheet.user_id != proposal.user_id or envelope.get("order_sheet_fingerprint") != order_sheet_fingerprint(sheet):
            validation = {"valid": False, "failures": ["Subject or artifact fingerprint mismatch"]}
    except ValueError as exc:
        validation = {"valid": False, "failures": [str(exc)[:2000]]}
    sources = [sorted(envelope.get("source_recommendation_keys") or []), sorted(envelope.get("source_proposal_ids") or [])]
    intent = str(envelope.get("paper_intent_id") or (digest(sources) if any(sources) else "publication:" + str(proposal.id)))
    accounts, instruments, listing_identities = {}, {}, {}
    from argosy.config import get_settings
    funding = envelope.get("funding_account_id") or get_settings().order_sheet_funding_account_id
    explicit = envelope.get("execution_accounts") or {}
    for line in raw.get("lines", []):
        symbol = str(line.get("symbol") or "").upper()
        candidates = [p for p in seed["positions"].values() if p.get("symbol") == symbol]
        known = {p["account"] for p in candidates}
        account = explicit.get(symbol)
        if not account:
            account = (next(iter(known)) if len(known) == 1 else None) if line.get("action") in ("SELL", "TRIM") else funding
        if account:
            accounts[symbol] = str(account).strip().lower()
        matches = [key for key, ref in seed["identities"].items() if ref["symbol"] == symbol]
        listing = re.search(r"\b" + re.escape(symbol) + r"\.(L|SW|AS|MI|DE)\b",
                            str((line.get("evidence") or {}).get("price_source") or ""))
        if listing:
            provider = listing[0]
            exact = [key for key in matches if seed["identities"][key].get("provider") == provider]
            instrument = exact[0] if len(exact) == 1 else None if exact else provider
            if instrument:
                listing_identities[instrument] = {"instrument": instrument, "symbol": symbol, "provider": provider}
        elif matches:
            instrument = matches[0] if len(matches) == 1 else None
        else:
            country = str((line.get("evidence") or {}).get("incorporation_country") or "").upper()
            foreign_fund = line.get("instrument_type") == "etf" and country not in ("US", "USA", "UNITED STATES")
            instrument = None if foreign_fund else symbol.replace("/", "-")
            if instrument:
                listing_identities[instrument] = {"instrument": instrument, "symbol": symbol, "provider": instrument}
        instruments[symbol] = instrument
    withdrawn = proposal.status in ("superseded", "cancelled", "canceled")
    if envelope.get("additional_intent_id"):
        intent = digest([intent, envelope["additional_intent_id"]])
    observation_hash = digest([proposal.id, envelope, intent, accounts, instruments, withdrawn, source_time.isoformat()])
    previous = connection.execute(select(ShadowEvent.__table__).where(ShadowEvent.user_id == proposal.user_id,
        ShadowEvent.cohort_id == seed["cohort_id"], ShadowEvent.kind == "sheet",
        func.json_extract(ShadowEvent.payload_json, "$.publication_id") == proposal.id)
        .order_by(ShadowEvent.id.desc()).limit(1)).mappings().first()
    prior_payload = json.loads(previous["payload_json"]) if previous else {}
    if prior_payload.get("observation_hash") == observation_hash:
        return
    identity = digest([previous["id"] if previous else None, observation_hash])
    key = "sheet:" + identity
    # First-seen time never inherits an overwritten, potentially old timestamp.
    published_at = source_time if preexisting else max(now, source_time)
    payload = {"receipt_id": identity, "publication_id": proposal.id, "intent_id": intent,
        "observation_hash": observation_hash, "predecessor_event_id": previous["id"] if previous else None,
        "publication_revision": prior_payload.get("publication_revision", 0) + 1,
        "execution_key": digest([[(line.get("symbol"), line.get("action"), line.get("shares"), line.get("venue"))
            for line in raw.get("lines", [])], accounts, envelope.get("additional_intent_id")]),
        "published_at": published_at.isoformat(), "source_surfaced_at": source_time.isoformat(),
        "publication_time_basis": "known_preexisting_at_activation" if preexisting else "transactional_first_capture",
        "publication_date": published_at.astimezone(NY).date().isoformat(),
        "sheet": raw, "sheet_hash": digest(raw), "envelope_hash": digest(envelope), "validation": validation,
        "envelope": envelope, "source_intents": sorted(["recommendation:" + str(key) for key in sources[0]]
                                                         + ["proposal:" + str(key) for key in sources[1]]),
        "validator_version": VERSION, "accounts": accounts, "instruments": instruments, "listing_identities": listing_identities,
        "economics": envelope.get("paper_economics") or {}, "withdrawn": withdrawn}
    append_event(connection, user_id=proposal.user_id, cohort_id=seed["cohort_id"], event_key=key,
                 kind="sheet", payload=payload, effective_at=published_at, now=now)


def load_inputs(session, user_id):
    base = baseline_event(session.connection(), user_id)
    if base is None:
        return None, []
    events = session.scalars(select(ShadowEvent).where(ShadowEvent.user_id == user_id,
        ShadowEvent.cohort_id == base["cohort_id"]).order_by(ShadowEvent.id)).all()
    return json.loads(base["payload_json"]), events


def project(session, *, user_id):
    if not session.in_transaction():
        session.execute(text("BEGIN IMMEDIATE"))
    seed, events = load_inputs(session, user_id)
    if seed is None:
        raise ValueError("Activate the comparison first")
    markets, receipts, flows = {}, [], []
    signature = []
    for row in events:
        if row.kind not in {"baseline", "sheet", "market", "flow"}:
            continue
        signature.append(row.content_hash)
        payload = json.loads(row.payload_json)
        if row.kind == "market":
            markets[payload["date"]] = payload
        elif row.kind == "sheet":
            receipts.append(payload)
        elif row.kind == "flow":
            flows.append(payload)
    result = replay(seed, list(markets.values()), receipts, flows)
    result.update(cohort_id=seed["cohort_id"], input_hash=digest(signature), receipts=len(receipts))
    append_event(session.connection(), user_id=user_id, cohort_id=seed["cohort_id"],
        event_key="projection:" + result["input_hash"], kind="projection", payload=result,
        effective_at=datetime.now(UTC))
    return result


def report(session, user_id):
    from argosy.services.shadow_calendar import next_session
    base = baseline_event(session.connection(), user_id)
    if base is None:
        return {"status": "not_started", "version": VERSION}
    seed = json.loads(base["payload_json"])
    row = session.scalar(select(ShadowEvent).where(ShadowEvent.user_id == user_id,
        ShadowEvent.cohort_id == base["cohort_id"], ShadowEvent.kind == "projection")
        .order_by(ShadowEvent.id.desc()).limit(1))
    projection = json.loads(row.payload_json) if row else {"status": "awaiting_baseline", "history": [], "applications": []}
    history = projection.get("history", [])
    return {"version": VERSION, "cohort_id": base["cohort_id"], "status": projection["status"],
        "snapshot_id": seed["snapshot_id"], "activation_at": seed["activation_at"],
        "baseline_date": projection.get("baseline_date"), "recorded_liquid_usd": seed["recorded_liquid_usd"],
        "scheduled_baseline_date": next_session(seed["activation_date"]),
        "starting_nav_usd": projection.get("starting_nav_usd"),
        "position_count": len(seed["positions"]), "cash_accounts": list(seed["cash"].values()),
        "excluded": seed["excluded"], "warnings": seed["warnings"], "policy": seed["policy"],
        "latest": history[-1] if history else None, "history": history,
        "applications": projection.get("applications", []), "pending_receipts": projection.get("pending_receipts", []),
        "returns_basis": "Daily time-weighted returns; common external flows at session close; not actual fills or proven alpha"}


def record_external_flow(session, *, user_id, receipt_id, account, ccy, amount, effective_at, evidence_reference):
    """Explicit external deposit only. A sale, transfer or sheet budget is NOT a deposit.

    Ingest callers must supply their confirmed receipt reference. This function
    neither guesses flows from portfolio changes nor moves any real money.
    """
    from argosy.services.shadow_calendar import calendar, close_at, next_session
    base = baseline_event(session.connection(), user_id)
    if not base or not str(evidence_reference).strip() or not str(receipt_id).strip():
        raise ValueError("An active cohort and explicit external receipt evidence are required")
    seed = json.loads(base["payload_json"])
    at = aware(effective_at)
    if at < aware(seed["activation_at"]) or money(amount) <= 0:
        raise ValueError("Only positive post-activation external deposits are supported; withdrawals require explicit liquidation instructions")
    day = at.astimezone(NY).date().isoformat()
    session_day = max(next_session(seed["activation_date"]),
        day if calendar().is_session(day) and close_at(day) >= at else next_session(day))
    payload = {"id": str(receipt_id), "account": str(account).strip().lower(), "currency": currency(ccy),
        "amount": str(money(amount)), "date": session_day, "effective_at": at.isoformat(),
        "evidence_reference": str(evidence_reference), "kind": "external_deposit"}
    return append_event(session.connection(), user_id=user_id, cohort_id=base["cohort_id"],
        event_key="flow:" + digest(str(receipt_id)), kind="flow", payload=payload, effective_at=at)

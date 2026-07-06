"""One-off: fold the 2026-07-06 executed Leumi deploy into the snapshot store.

Ariel EXECUTED the deploy himself (11 broker fills, all full, Leumi USD,
2026-07-06, total $161,376.10). This script:

1. Applies the fills to the latest ``portfolio_snapshots`` row via
   ``argosy.services.snapshot_refresh.apply_fills_to_snapshot`` and inserts a
   new row with ``source_path='fills-applied:2026-07-06-deploy'``.
2. Closes the accepted deploy-team flags (action_proposals 38 CSPX, 41 FUSA,
   43 IWQU) as status='accepted' — Ariel accepted by executing (CSPX at a
   reduced $36,362 vs the flagged $42,000).
3. Writes a closed-loop EXPECTATION marker (internal ``note_only`` row,
   status='rejected' = never user-visible, same pattern as the cooldown
   markers) + expectation lines in the new snapshot's parse_warnings: the
   NEXT real broker/TSV ingest must show the 8 new positions, the topped-up
   CSPX/EIMI share counts, and the reduced Leumi USD cash.
4. Prints old/new totals, the new cash balance, and a per-sleeve
   current-vs-v67-target reconciliation table.

Run:  .venv/Scripts/python.exe scripts/apply_fills_2026_07_06_deploy.py
Idempotent: aborts if the fills-applied row already exists.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from argosy.services.snapshot_refresh import Fill, apply_fills_to_snapshot
from argosy.state.models import ActionProposal, PortfolioSnapshotRow

DB_URL = "sqlite:///db/argosy.db"
SOURCE_TAG = "fills-applied:2026-07-06-deploy"
USER_ID = "ariel"
TODAY = date(2026, 7, 6)

# Broker prints — Leumi, USD, 2026-07-06, all full fills. Symbols use the
# snapshot convention (bare ticker, LSE hint in details for the repricer).
# asset_type = the v67 sleeve's snapshot_category for the instrument.
FILLS = [
    Fill("SPMV", 125, 114.70, "Defensive", "(ISHR SP500 MIN VOL) SPMV LN"),
    Fill("TEM", 80, 60.46, "Individual Stocks", "(Tempus AI) TEM"),
    Fill("OKLO", 100, 51.32, "Individual Stocks", "(Oklo) OKLO"),
    Fill("RXRX", 1500, 3.78, "Individual Stocks", "(Recursion Pharma) RXRX"),
    Fill("IBTA", 2000, 5.96, "Defensive", "(ISHR $ TREA 1-3Y) IBTA LN"),
    Fill("FUSA", 800, 16.27, "Dividend", "(FID US QUAL INC) FUSA LN"),
    Fill("EIMI", 250, 55.07, "Broad Index", "(ISHR CORE EM IMI) EIMI LN"),
    Fill("IWQU", 225, 88.42, "Growth", "(ISHR MSCI WLD QUAL) IWQU LN"),
    Fill("EXUS", 800, 45.55, "International", "(XTR MSCI WLD EXUSA) EXUS LN"),
    Fill("CSPX", 45, 808.04, "Core Equity", "(ISHR CORE S&P500) CSPX LN"),
]
TOTAL_COST = sum(f.cost for f in FILLS)

FLAG_CLOSURES = {  # action_proposals ids Ariel accepted by executing
    38: "CSPX",
    41: "FUSA",
    43: "IWQU",
}
DECIDED_NOTE = "executed 2026-07-06 (CSPX at reduced size $36,362)"


def main() -> None:
    engine = sa.create_engine(DB_URL)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    # ---- idempotency guard --------------------------------------------------
    existing = session.execute(
        sa.select(PortfolioSnapshotRow.id).where(
            PortfolioSnapshotRow.source_path == SOURCE_TAG
        )
    ).first()
    if existing:
        print(f"ABORT: {SOURCE_TAG!r} already applied (row id {existing[0]}).")
        return

    # ---- expectation lines (closed-loop: next real ingest must confirm) ----
    new_syms = [f.symbol for f in FILLS if f.symbol not in ("CSPX", "EIMI")]
    expectations = [
        "expectation:next-real-ingest:new positions expected at Leumi/USD: "
        + ", ".join(
            f"{f.symbol} {f.shares:g} sh" for f in FILLS if f.symbol in new_syms
        ),
        "expectation:next-real-ingest:CSPX 240 sh total, EIMI 650 sh total",
        f"expectation:next-real-ingest:Leumi USD cash reduced by "
        f"${TOTAL_COST:,.2f} vs the 2026-06-29-sourced balance; a mismatch "
        f"must surface loudly (deploy executed 2026-07-06)",
    ]

    res = apply_fills_to_snapshot(
        session,
        fills=FILLS,
        source_tag=SOURCE_TAG,
        user_id=USER_ID,
        cash_location="Leumi",
        cash_currency="USD",
        extra_warnings=expectations,
        today=TODAY,
        commit=True,
    )

    # ---- close the accepted deploy-team flags ------------------------------
    now = datetime.now(timezone.utc)
    closed = []
    for pid, sym in FLAG_CLOSURES.items():
        row = session.get(ActionProposal, pid)
        if row is None:
            print(f"WARN: action_proposal {pid} ({sym}) not found")
            continue
        if row.status != "open":
            print(f"WARN: action_proposal {pid} ({sym}) status={row.status!r}, skipping")
            continue
        row.status = "accepted"
        row.decided_at = now
        row.decided_by_user_note = DECIDED_NOTE
        closed.append((pid, sym))
    session.commit()

    # ---- internal closed-loop expectation marker (cooldown-marker pattern) --
    marker = ActionProposal(
        user_id=USER_ID,
        summary="(closed-loop marker: 2026-07-06 deploy expectation)",
        rationale_md=(
            "Ariel executed the 2026-07-06 Leumi deploy (11 fills, "
            f"${TOTAL_COST:,.2f}). Snapshot row {res.row.id} "
            f"({SOURCE_TAG}) carries the fills. EXPECTATION: the next real "
            "TSV/broker ingest shows the 8 new positions "
            f"({', '.join(new_syms)}), CSPX 240 sh, EIMI 650 sh, and Leumi "
            "USD cash reduced accordingly. A mismatch must surface loudly. "
            "NOTE: applying the fills overdrew the stale snapshot cash "
            f"(balance {res.cash_after_local:,.2f} USD) — the real broker "
            "balance was higher than the carried 2026-06-29 figure; the "
            "next ingest reconciles it."
        ),
        suggested_payload=json.dumps(
            {
                "kind": "closed_loop_expectation",
                "deploy_date": "2026-07-06",
                "snapshot_row_id": res.row.id,
                "expected_new_positions": {
                    f.symbol: f.shares for f in FILLS if f.symbol in new_syms
                },
                "expected_totals": {"CSPX_shares": 240, "EIMI_shares": 650},
                "cash_reduction_usd": round(TOTAL_COST, 2),
            }
        ),
        severity="info",
        surfaced_at=now,
        expires_at=now + timedelta(days=60),
        status="rejected",  # marker: never user-visible (cooldown-marker pattern)
        decided_at=now,
        decided_by_user_note="closed_loop_expectation_marker: 2026-07-06 deploy",
        kind="note_only",
        dedup_key="closed-loop-expectation:fills-2026-07-06-deploy",
        execution_state="dismissed",
    )
    session.add(marker)
    session.commit()

    # ---- report -------------------------------------------------------------
    print(f"snapshot row inserted: id={res.row.id} source_path={SOURCE_TAG}")
    print(f"old total: ${res.old_total_usd_k * 1000:,.2f}")
    print(f"new total: ${res.new_total_usd_k * 1000:,.2f}")
    print(
        f"delta:     ${(res.new_total_usd_k - res.old_total_usd_k) * 1000:,.2f}"
        "  (held-position fills revalued at snapshot quotes vs fill prints)"
    )
    print(
        f"Leumi USD cash: ${res.cash_before_local:,.2f} -> "
        f"${res.cash_after_local:,.2f}"
    )
    for w in res.warnings:
        print(f"WARNING: {w}")
    print(f"merged: {res.merged}  added: {res.added}")
    print(f"flags closed as accepted: {closed}")
    print(f"expectation marker: action_proposal id={marker.id}")

    _print_sleeve_table(session, res.snapshot.positions)
    session.close()


def table_only() -> None:
    """Re-print the reconciliation table from the already-applied row."""
    engine = sa.create_engine(DB_URL)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    row = session.execute(
        sa.select(PortfolioSnapshotRow).where(
            PortfolioSnapshotRow.source_path == SOURCE_TAG
        )
    ).scalar_one()
    from argosy.ingest.tsv import PortfolioPosition

    positions = [PortfolioPosition(**d) for d in json.loads(row.positions_json)]
    _print_sleeve_table(session, positions)
    session.close()


def _print_sleeve_table(session, positions) -> None:
    """Per-sleeve current % (new snapshot) vs plan v67 target %.

    Attribution: symbol->sleeve from v67 instruments first, then an
    approximate asset_type->sleeve map. Legacy single stocks (AMD, GOOG,
    AMZN, META, SOFI, RKT, TSLA...) are NOT in any v67 sleeve and are shown
    on their own line, not inside high-growth. SGOV counts as Cash &
    T-bills (that is the sleeve's economic content), REIT/property lines as
    Real assets. Denominator = tradeable book (minus the direct
    real-estate line), matching the plan's 'full tradeable book' basis
    approximately.
    """
    row = session.execute(
        sa.text(
            "SELECT target_allocation_json FROM plan_versions "
            "WHERE role='current' ORDER BY id DESC LIMIT 1"
        )
    ).first()
    classes = json.loads(row[0])["classes"]
    sym_to_sleeve = {
        i["symbol"].upper(): c["label"]
        for c in classes
        for i in c.get("instruments", [])
    }
    cash_sleeve = "Cash & T-bills (incl. ILS tranche)"
    # Approximate asset_type -> sleeve for positions v67 names no instrument
    # for. Individual Stocks deliberately maps to a NON-sleeve line: the
    # legacy single names are not the high-growth sleeve.
    cat_to_sleeve = {
        "Core Equity": "US broad-market core",
        "Dividend": "Dividend-quality income",
        "International": "International developed (ex-US)",
        "Broad Index": "International developed (ex-US)",  # STOXX 600 line
        "Growth": "Global quality growth (ex-NVDA-dense)",
        "Defensive": "US low-volatility equity",
        "REIT": "Real assets (REIT/TIPS)",
        "Real Estate": "Real assets (REIT/TIPS)",  # IWDP property ETF
        "Individual Stocks": "Legacy single stocks (no v67 sleeve)",
        "Equity": "Legacy single stocks (no v67 sleeve)",
    }
    symbol_overrides = {"SGOV": cash_sleeve}  # T-bill reserve = the cash sleeve

    tradeable = [
        p
        for p in positions
        if not (
            (p.asset_type or "").strip().lower() in ("real estate",)
            and (p.symbol or "-").strip() in ("-", "")
        )
    ]
    total_k = sum(p.usd_value_k or 0.0 for p in tradeable)

    by_sleeve: dict[str, float] = {}
    for p in tradeable:
        sym = (p.symbol or "").strip().upper()
        at = (p.asset_type or "").strip()
        if at.lower() == "cash":
            sleeve = cash_sleeve
        elif sym == "NVDA":
            sleeve = "Strategic single-stock (NVDA)"
        elif sym in symbol_overrides:
            sleeve = symbol_overrides[sym]
        elif sym in sym_to_sleeve:
            sleeve = sym_to_sleeve[sym]
        elif at in cat_to_sleeve:
            sleeve = cat_to_sleeve[at]
        else:
            sleeve = f"Unmapped ({at or '?'})"
        by_sleeve[sleeve] = by_sleeve.get(sleeve, 0.0) + (p.usd_value_k or 0.0)

    targets = {c["label"]: c["target_pct"] for c in classes}
    print(f"\nPer-sleeve reconciliation vs plan v67 (tradeable ${total_k * 1000:,.0f}):")
    print(f"{'sleeve':48} {'current%':>9} {'target%':>8} {'delta%':>8} {'usd_k':>10}")
    for sleeve in sorted(
        set(by_sleeve) | set(targets), key=lambda s: -by_sleeve.get(s, 0.0)
    ):
        cur = 100.0 * by_sleeve.get(sleeve, 0.0) / total_k
        tgt = targets.get(sleeve)
        tgt_s = f"{tgt:8.2f}" if tgt is not None else "       -"
        delta_s = f"{cur - tgt:+8.2f}" if tgt is not None else "       -"
        print(
            f"{sleeve:48} {cur:9.2f} {tgt_s} {delta_s} "
            f"{by_sleeve.get(sleeve, 0.0):10.1f}"
        )


if __name__ == "__main__":
    import sys

    if "--table" in sys.argv:
        table_only()
    else:
        main()

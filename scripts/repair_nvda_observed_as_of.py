#!/usr/bin/env python
"""Surgical one-row repair: NVDA quantity date 2026-08-08 -> 2026-07-13.

Last night a (now-fixed) refresh bug re-stamped NVDA's ``observed_as_of``
(the quantity-observation date) to the run date 2026-08-08, disarming the
90-day quantity-staleness guard on a ~$2.45M / 58%-of-book position. The
true Schwab quantity date is 2026-07-13.

This script flips ONLY ``observed_as_of`` (2026-08-08 -> 2026-07-13) in the
two durable places that carry it:

  (a) the active NVDA row in ``unmanaged_holdings``;
  (b) the NVDA element inside the current head snapshot's ``positions_json``
      (``portfolio_snapshots``, head chosen by imported_at DESC, id DESC —
      the same ordering as get_latest_snapshot_row()).

It does NOT touch ``valued_as_of`` (the legitimate fresh mark), shares,
price, value, or any other field/row. It is IDEMPOTENT: it only rewrites
cells whose observed_as_of currently equals the corrupted OLD_DATE, so a
second run is a no-op and it will never clobber a legitimately different
date.

USAGE (operator, backend STOPPED):
    python scripts/repair_nvda_observed_as_of.py --db db/argosy.db --apply

Without --apply it runs read-only and prints what it WOULD change.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

SYMBOL = "NVDA"
OLD_DATE = "2026-08-08"
NEW_DATE = "2026-07-13"


def _head_snapshot(cur: sqlite3.Cursor, user_id: str):
    """Return (id, positions_json) of the head snapshot, mirroring
    portfolio_snapshot_store.get_latest_snapshot_row ordering."""
    return cur.execute(
        "SELECT id, positions_json FROM portfolio_snapshots "
        "WHERE user_id = ? ORDER BY imported_at DESC, id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


def _patch_positions(positions_json: str):
    """Return (new_json, changed_count, before_after) flipping only the NVDA
    element's observed_as_of OLD_DATE->NEW_DATE. Preserves list order and all
    other fields/positions byte-for-byte via json round-trip."""
    positions = json.loads(positions_json)
    changed = 0
    evidence = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        if str(pos.get("symbol", "")).upper() != SYMBOL:
            continue
        before = pos.get("observed_as_of")
        # capture the sibling we must NOT touch, for the audit trail
        valued_before = pos.get("valued_as_of")
        if before == OLD_DATE:
            pos["observed_as_of"] = NEW_DATE
            changed += 1
            evidence.append(
                {
                    "observed_as_of_before": before,
                    "observed_as_of_after": NEW_DATE,
                    "valued_as_of_untouched": valued_before,
                    "shares_untouched": pos.get("shares"),
                }
            )
    # match persist_snapshot's serialization: json.dumps(..., default=str),
    # default separators. Values came from json.loads so all are JSON-native.
    return json.dumps(positions, default=str), changed, evidence, len(positions)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="db/argosy.db", help="path to the sqlite db")
    ap.add_argument("--user", default="ariel", help="user_id owning the snapshot")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write the repair. Omit for a read-only dry run.",
    )
    args = ap.parse_args()

    mode = "rw" if args.apply else "ro"
    con = sqlite3.connect(f"file:{args.db}?mode={mode}", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    print(f"DB: {args.db}   mode: {mode}   user: {args.user}")
    print("-" * 60)

    # ---- (a) unmanaged_holdings ------------------------------------------
    # SCOPED to (user_id, status='active'): an unscoped UPDATE on symbol +
    # observed_as_of alone would rewrite ANY user's — or a RETIRED — NVDA row
    # that happens to carry the corrupted date in a multi-tenant/historical DB
    # (Sol BLOCK-7). The snapshot side was already user-scoped; match it here.
    uh_rows = cur.execute(
        "SELECT id, symbol, shares, valued_as_of, observed_as_of, location "
        "FROM unmanaged_holdings "
        "WHERE user_id = ? AND symbol = ? AND status = 'active' "
        "AND observed_as_of = ?",
        (args.user, SYMBOL, OLD_DATE),
    ).fetchall()
    print(
        f"[unmanaged_holdings] active rows for user={args.user} matching "
        f"{SYMBOL} observed_as_of={OLD_DATE}: {len(uh_rows)}"
    )
    for r in uh_rows:
        print(
            f"  id={r['id']} shares={r['shares']} "
            f"observed_as_of {r['observed_as_of']} -> {NEW_DATE} "
            f"(valued_as_of stays {r['valued_as_of']})"
        )

    # ---- (b) head snapshot positions_json --------------------------------
    head = _head_snapshot(cur, args.user)
    if head is None:
        print("[portfolio_snapshots] no snapshot for user — aborting")
        con.close()
        return 2
    sid = head["id"]
    new_json, json_changed, evidence, npos = _patch_positions(head["positions_json"])
    print(
        f"[portfolio_snapshots] head id={sid}  positions={npos}  "
        f"NVDA elements to flip: {json_changed}"
    )
    for e in evidence:
        print(f"  {e}")

    total = len(uh_rows) + json_changed
    if total == 0:
        print("\nNothing to do — already repaired (idempotent no-op).")
        con.close()
        return 0

    if not args.apply:
        print("\nDRY RUN — no writes. Re-run with --apply to persist.")
        con.close()
        return 0

    # ---- apply -----------------------------------------------------------
    cur.execute(
        "UPDATE unmanaged_holdings SET observed_as_of = ? "
        "WHERE user_id = ? AND symbol = ? AND status = 'active' "
        "AND observed_as_of = ?",
        (NEW_DATE, args.user, SYMBOL, OLD_DATE),
    )
    uh_written = cur.rowcount
    if json_changed:
        cur.execute(
            "UPDATE portfolio_snapshots SET positions_json = ? WHERE id = ?",
            (new_json, sid),
        )
    con.commit()
    print(f"\nAPPLIED: unmanaged_holdings rows={uh_written}, snapshot {sid} positions_json rewritten={bool(json_changed)}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

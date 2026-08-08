# Post-restore: recomputed numbers + fix-spec punch-list (for the repair agent)

**From:** Claude reviewer session. **For:** the agent that owns `holding_books.py`
/ `snapshot_refresh.py` / the ingest paths. **All analysis read-only** against
`db/argosy.db` (snapshot 52 = current head); nothing edited, no DB writes. This is
the two deliverables you're owed after the restore: (A) the downstream numbers
recomputed on the true book, and (B) a ready-to-apply fix punch-list for the three
residual defects — with one correction to the finding-3 figure in your own note.

---

## PART A — Downstream numbers recomputed (the point of the repair)

Every figure below was last computed on the corrupted Leumi-only book ($1,615.6k,
NVDA absent). None had been recomputed on the restored book. Computed read-only
with the system's own modules; FX 2.9981 both sides.

| Metric | Stale ($1.6M book) | Restored (true) | Delta |
|---|---|---|---|
| Net worth USD | $1,562,469 | **$4,069,298** | +$2,506,829 |
| Net worth NIS | ₪4,684,438 | **₪12,200,161** | +₪7,515,723 |
| NVDA % of investable | **0.00%** | **59.86%** | +59.86pp |
| NVDA % of total book | 0.00% | 58.46% | +58.46pp |
| NVDA vs cap(13%)/target(8%) | under cap | **4.6× cap, 7.5× target** | — |
| US-situs estate base | $757,409 | $3,258,344 | +$2.50M |
| **US-situs 40% NRA liability** | $278,964 (₪836k) | **$1,279,338 (₪3,835,583)** | **+$1.0M** |
| Estate-safe (UCITS/ILS) | $835,083 | $835,083 | $0 |
| Retirement vs FI target (₪11.84M) | ₪4.68M = **39.6%** | ₪12.20M = **103.1%** | short → at-the-line |
| FX exposure | USD 99.6% | USD 99.6% | ~0 |

**Smoking gun — the corruption is baked into the live plan:** plan **v93**
(`plan_versions.horizon_medium_json`) stores *"NVDA current weight = 0.0%"*. The
deconcentration surfaces were computed with NVDA at zero. **Plan v93 needs
re-derivation on the restored book** — it is the top open item.

**Three things that materially changed for the owner:**
1. **NVDA grossly over the cap** — 59.86% of investable vs a 13% cap. Deconcentration
   / sell-quota is the top binding constraint again; every "NVDA 0%" surface is wrong.
2. **US-situs estate exposure crossed into danger** — NRA 40% tail ~$279k → **~$1.28M**,
   almost all NVDA. The estate gate should re-fire. (UCITS mitigation unchanged — the
   corruption hid the exposure, not the mitigation.)
3. **Retirement flips** — ₪4.68M (40% of FI) → ₪12.20M (103%); earliest-safe-retirement
   moves materially earlier.

**Caveats gating action on these numbers:**
- **Full sequence-aware MC not run** (`canonical_feasible_dual_track`, heavy) — retirement
  is directional (deterministic FI target vs net worth). Run the real MC before any
  retirement-date claim.
- **NVDA quantity is 26-days-stale** (`observed_as_of` restamped today — Finding 2).
  Verify 10,940 sh against a fresh Schwab CSV for any post-07-13 sale before sizing the
  concentration/estate/quota figures — a sale would lower all three.
- **Net worth excludes ~$122k of real-estate equity** ($69k Aborad row + REIT ETFs
  O/IWDP) — pre-existing, both books.

---

## PART B — Fix punch-list (findings ranked by money/data risk)

### Finding 3 — HIGH — silent $5,446.93 net-worth/estate undercount (dedupe collision) — PROVEN
**Correction to your note:** it is **not** the $17.66k row that drops. Root cause is
`dedupe_positions_by_symbol_location` (`argosy/services/holding_books.py:505-513`): a
blank symbol keys on `("", loc)`, so the **two** Leumi Cash lots both map to
`('','leumi')` — the first ($17,662.11) is kept, the **second ($5,446.93) is silently
dropped** (`continue`, no sum). `load_total_book` returns 45 of 46.
- **Blast radius:** `load_total_book` is the single entry point for every net-worth /
  estate / concentration surface (`portfolio.py:296`, `net_worth_bases.py:156`,
  `plan_numeric_resolver.py:1520/1605/1939`, `retirement/safety_gates.py:165`,
  `nvda_projection.py:192`, …). The $5,446.93 is invisible to all of them, and
  `books_consistency_check_positions` (`:1937`) `continue`s on blank symbols → **no
  degrade flag** (silent conservation breach).
- **Fix (minimal):** give each blank-symbol row a per-occurrence anon key (mirror the
  `__anon:` pattern at `merge_positions_per_account:434-437`):
  `key = ("", f"__anon:{len(order)}")`. Named symbols still dedupe (NVDA double-count
  guard intact); distinct unnamed cash/RE lots never collapse. They stay
  unmanaged/untradeable via existing `is_managed_position`.

### Finding 2 — MEDIUM-HIGH — self-refresh re-dates carried quantities → perpetually disarms the staleness guard — PROVEN
Root cause: `merge_positions_per_account` (`holding_books.py:377-378`) unconditionally
overwrites `observed_as_of`/`valued_as_of` with the incoming (today's) date. Restore
stamped NVDA `2026-07-13`; snapshot 52 + the durable row now read `2026-08-08`.
- **Correction to the premise:** `quantity_is_stale` does **not** still return True — it
  returns False for both dates (26 d < 90-d `QUANTITY_STALE_DAYS`). So there is **no
  current mis-valuation** (values come from live reprice / `mark_is_stale`).
- **The real damage:** (a) **provenance corruption** — the durable book asserts NVDA's
  quantity was observed today when it's 07-13; (b) **latent systemic guard defeat** —
  daily self-refresh re-stamps `observed_as_of=today` on every carried holding, so the
  90-day quantity-staleness gate (`:903, :972`) can **never fire** for any repriced-but-
  not-reingested position. NVDA (58%) would read fresh indefinitely.
- **Fix (minimal — reprice PRICE, not the QUANTITY date):**
  `d["observed_as_of"] = _as_date(d.get("observed_as_of")) or incoming_date` (preserve a
  carried date; a real TSV feed carries none, so it still stamps the feed date). Keep
  `valued_as_of = today` (the legitimate fresh mark).

### Finding 4 — LOW — six `parse_portfolio_tsv` bypasses are read-only; NO re-truncation risk — PROVEN
`nvda_sales_history:341`, `cli/critique:92`, `cli/ingest:35`, `daily_brief:140`,
`daily_brief_runner:258`, `leumi_tsv:46` — all read the raw TSV, none persist a
snapshot. **No conservation risk.** Residual: read-*consistency* only (they read the
latest raw TSV under `ARGOSY_HOME`, not the guarded/restored DB book) — route them
through `get_latest_snapshot_row`/`load_total_book`. (Two also read non-existent attrs
`p.ticker`/`p.quantity` → blank tickers; pre-existing, try/except-wrapped.) Low priority.

---

## PART C — The owed sequence (highest-value first)
1. **Re-derive plan v93 on the restored book** — it stores NVDA 0%; deconcentration,
   concentration cap, estate gate and retirement all key off it. This is the deliverable.
2. **Fix Finding 3** (the $5,446.93 silent drop) — one-line key change, HIGH.
3. **Fix Finding 2** (stop re-dating carried quantities) before trusting NVDA-derived
   figures — re-arms the staleness guard.
4. **Run the full sequence-aware retirement MC** on the true book (the deterministic
   flip short→at-the-line needs MC confirmation).
5. **Full-suite run** (with `pytest-timeout`, excluding the two known hangers) — master
   merged on 167 targeted tests, not a green suite.
6. **Owner:** pull a fresh Schwab/Aborad statement to close the carry-forward assertion
   and confirm the NVDA 10,940 quantity.
7. Finding 4 read-consistency routing; name the opaque `-` rows.

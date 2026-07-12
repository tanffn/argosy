# Handover — 2026-07-12 · expense-ingest hardening + UX block · next: inbox & portfolio

**Roles:** resident Claude Code session = REVIEWER (see 2026-07-11 handover §4 — contract
unchanged: implementer works on `feat/opens-2026-07-11`, hands back per block, announces
every commit, NEVER merges; reviewer verifies evidence, merges on PASS).
**Git at close:** master = origin = branch = `a738971`. Backend under supervisor
(`scripts/start_backend_detached.ps1`); UI dev on 1337. Migrations head **0090**.

## 1. What merged today (each reviewed; see git log for the trail)

- **UX block (§7 of the 07-11 handover)**: verdict provenance on judgment surfaces (falsifier
  state / clock / last-fleet-check), Home greeting dirty-flag (regen on material events),
  multi-option decision cards (choice persists; directive feed carries the CHOICE).
- **Expense-ingest marathon (owner-driven, live bugs)**:
  - Cal rolling 90-day format (sheet 'פירוט עסקאות וזיכויים') parses; card 6225 = **Cal**,
    2923 = **Max** (brands corrected; parser modules keep FORMAT names — documented in
    sniff.py, do not 'fix' backwards).
  - Overlap dedup: rolling/bank-range ingests dedup SOURCE-wide (reference-aware;
    installments exempt — they legitimately repeat across monthly statements).
  - Parse-sanity gate (fatal on nonsense, atomic abort): implementer block + reviewer
    redesign of the date anchor (absolute floor 2000 + future ceiling + span cap on
    fixed-window statements ONLY; backfills and range exports pass).
  - Isracard 2026-07 format: new PENDING mini-table decoy made the parser read 1 of 16 rows
    (live bug, 1266_07_2026.xlsx) — parser+oracle now anchor on 'עסקאות למועד חיוב';
    pending rows skipped BY DESIGN (double-count + conservation).
  - Rolling exports SELF-IDENTIFY card last-4 from the title; card_last4 prompt only for
    monthly max-format files.
  - Upload card: per-source issuer login quick-links (0090 `login_url` + cardholder names —
    tenant data in DB, not code).
- **Ground-truth suite green against REAL samples for the first time** (fixtures no longer
  glob portfolio/USD artifacts into NIS tests): 65/65 with ARGOSY_EXPENSE_SAMPLES_ROOT set.

## 2. Open items / queues

- **CMPS**: card 15 un-shadowed, Ariel approving; expires **2026-07-13 15:30**. VOR expiring
  by choice. Verify fill + position after approval; next statement upload auto-verifies.
- **Alpha predictions (G3)**: July's 62 score from 07-13 — VERIFY.
- **Weekly critique of v89-v91** — read its first run (independent check on the reviewer's
  mechanical closes; v90=8% high-growth sleeve owner decision, v91=paced NVDA tranche).
- **Verdict registry live** (0087, 8 seeds; OKLO dated trigger 2026-07-31). Trigger loop
  fixed twice (tick kwarg, default quotes fn) — follow-up: quote fetch for NON-HELD subjects
  (price triggers inert until then; dated triggers fine).
- **Stance falsifiers all NULL (53 rows)** — provenance UI shows the warning honestly;
  tier-1 mechanical fill (instrument_meta exit_triggers, verdicts, decision runs) queued;
  tier-2 fleet authoring pass = token-gated.
- **Credit-card format audit** — prompt issued 2026-07-12 (see session log / §3 below).
- Historical cross-statement duplicate audit (38/3/23/22 groups; installment-aware) — queued.
- Token-gated queue: verify-run on item-A close, first token-contract synthesis, Fable-5
  trader promotion decision (evidence in runs/2026-07-12-fable-ab*, owner kept Opus for now),
  sdc re-burn + omk re-score, RKT/NOW evals (item H), streams D/C/E (item E).
- Reboot gap: supervisor doesn't survive restart — register scripts/start_backend_detached.ps1
  as a startup task (queued; owner restarted manually today).

## 3. NEXT SESSION FOCUS (owner, 2026-07-12): INBOX + PORTFOLIO

Owner said: "we will resume working on inbox and portfolio". Session start: ask Ariel for
the concrete gripes/goals, then check these known threads first: multi-option cards shipped
(does row-72-class rendering satisfy?), verdict provenance surfaces (falsifier warning
state), funnel-proposal graduation design (shadow → approvable; queued), portfolio
discovery-cards with clocks, greeting dirty-flag behavior on visit.

# /life-events redesign as cashflow phase modeler — Design (Spec D)

**Status:** Pending Ariel approval. Codex tandem zigzag review: BLOCK → APPROVE_WITH_CONDITIONS after 4 BLOCKERs + 6 IMPORTANTs integrated (see §Codex tandem review summary at end).
**Date:** 2026-05-29
**Author:** Claude (Opus 4.7), with codex tandem zigzag review (run 2026-05-29).
**Codex session:** `tools/codex-tandem/sessions/2026-05-29-life-events-redesign-spec-review/`.
**Sibling specs:** [`2026-05-29-plan-execute-monitor-reorg-design.md`](2026-05-29-plan-execute-monitor-reorg-design.md) (sprint #1 landed life_events as a date-constrained table) and [`2026-05-29-anomaly-detection-rsu-prevest-design.md`](2026-05-29-anomaly-detection-rsu-prevest-design.md) (sprint #2 added the `<UpcomingVestCard>` "Add as life event →" CTA that prefills the form).

## Motivation

The current `/life-events` implementation — landed in sprint #1 commits `422eb3d` (migration 0042) and `a5b4236` (page + service) — models life events as **date-constrained retirement-age clamps**. A `retirement_milestone:target_retire_year_change` row shifts `effective_retire_ready_age()` outward; a `expense_event` row is described as a "blocking clamp." That's the wrong model. The user's expenses don't determine when he can retire — his **money** does. Life events should affect **forward cashflow**, which then propagates through the projection to retirement feasibility.

Ariel's framing, verbatim:

> "Life events should model how expenses change across phases of life (kids leave home → lower expenses, wedding → one-shot $50K, car every 5y → recurring), NOT retirement-age constraints. **I retire from work not life.** Expenses are not fixed 30+ years into the future."

Concrete examples from the user:

> "Kids leave home I pay less. Kids get married I might need to support with 50k. Every 5 years I want a new car, ~250k NIS."

All cashflow-impacting. None are retirement-age constraints. The clamp branch in `argosy/services/cashflow_projection.py:effective_retire_ready_age()` (lines 879-986) is conceptually wrong and must go. The replacement model:

- **One-shot events** (wedding gift, inheritance, RSU vest landing as cash) — additive spike on the expense series (negative or positive) at the event date.
- **Recurring events** (new car every 5y, major renovation every 10y, kid's college every Sep for 4y) — periodic spikes anchored on an anchor date with a period.
- **Phase changes** (kids leave home → monthly expenses drop, partner retires → monthly income drops) — step function that shifts the baseline expense level from `phase_start_date` onward, optionally ending at `phase_end_date`.

The retire-ready age then comes ONLY from cashflow feasibility (with the RSU vest clamp still valid — RSUs ARE a money constraint, you can't sell them before they vest). Life events feed the expense series; the expense series feeds the projection; the projection's solvency crossing IS the retire-ready age. No second pathway.

## Goal

Ship a 5-7 commit sprint that:
1. Replaces the date-constraint life-event schema with a cashflow-shape schema (`delta_kind` discriminator).
2. Wires life events into the cashflow projection as expense-series modifiers (new pure function).
3. Removes the wrong-model `effective_retire_ready_age()` life-event clamp branch.
4. Migrates existing rows non-destructively (data migration inside the alembic migration).
5. Rewrites the UI form around the three cashflow shapes.
6. Preserves the `<HolisticTimelineCard>` markers + the RSU pre-vest CTA contract.

## Non-goals

- **No changes to the RSU clamp** in `effective_retire_ready_age()`. RSUs are a real liquidity constraint (you can't sell unvested shares); they stay clamped per sprint #1 §3.1.
- **No changes to `replan_triggers.life_event` kind** — the trigger still exists and still fires on life-event creation, because creating a life event still changes the cashflow shape and therefore the plan should re-compose. Only the SEMANTICS change (it's now "cashflow shape changed" not "retirement date changed").
- **No LLM-aided event categorization in v1.** The form is still Pydantic-enum-gated (loud-error contract from sprint #1 §4.1 preserved). If a future revision adds LLM categorization, accuracy-over-cost binds it to Opus per [[feedback_accuracy_over_cost]].
- **No deletion of the `category` enum** (career_event / family_event / asset_event / expense_event / recurring_expense / retirement_milestone). `category` continues to describe the user's INTENT axis; the new `delta_kind` describes the CASHFLOW SHAPE axis. The two are orthogonal — see §1.4 below for the interaction matrix.
- **No new currency.** Cashflow projection operates in NIS; life events store amounts in USD per existing convention. A constant FX is applied at projection time (the same FX the rest of `cashflow_projection.py` uses today — see §2.4 below for the explicit lookup path).

## Sprint commit table

Per [[feedback_work_style_long_sprints]] — long sprint, codex zigzag per risky commit, SDD update per commit, blockers logged via codex zigzag, not paused on user.

| # | Commit | Codex zigzag | Notes |
|---|---|---|---|
| 1 | Migration 0049 — `life_events` cashflow-shape extension + data migration of existing rows | **Yes** | Schema change + lossy-by-design data conversion. Risk: existing rows |
| 2 | `apply_life_event_deltas()` pure function in `cashflow_projection.py` + tests for all three delta_kinds + edge cases | **Yes** | Money math; critical correctness path |
| 3 | Wire `apply_life_event_deltas()` into `project_cashflow()` + remove the life-event clamp branch from `effective_retire_ready_age()` + update affected tests | **Yes** | Behavioral change to retire-ready computation; test surface impact |
| 4 | API DTO + service updates — new Pydantic schema with `delta_kind` discriminator, `field_rules_by_category` extended, 422 banner contract preserved | **Yes** | Validation contract; UI/backend interface |
| 5 | UI form rewrite — three sections (one-shot / recurring / phase-change), server-driven field visibility, `<UpcomingVestCard>` prefill contract updated | No | UI only; consumes catalog endpoint |
| 6 | `<HolisticTimelineCard>` markers updated — one-shot=dot, phase-change=vertical line, recurring=repeated dots | No | UI only; light edit |
| 7 | User-guide refresh + SDD §"Life events" section rewrite | No | Text only |

**Estimated:** 7 commits. If commit #1 needs to be split into "schema migration" + "data migration" per codex review precedent (sprint #1 spec commit #2 was split this way), the sprint becomes 8.

## Section 1 — Data model

### Section 1.1 — Schema extension (migration 0049)

The existing `life_events` table (migration 0042) carried:

```
id, user_id, category, kind, target_date, amount_usd, recurring_years,
description, source_id, created_at, updated_at
```

Migration 0049 adds the `delta_kind` discriminator and the per-shape amount/date fields, and migrates existing data per §1.5.

**Final schema:**

```sql
CREATE TABLE life_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,

  -- INTENT axis (existing, unchanged enum membership).
  category TEXT NOT NULL CHECK (category IN (
    'career_event','family_event','asset_event','expense_event',
    'recurring_expense','retirement_milestone'
  )),
  kind TEXT NOT NULL,

  -- CASHFLOW SHAPE axis (NEW — the heart of this redesign).
  delta_kind TEXT NOT NULL CHECK (delta_kind IN (
    'one_shot',                  -- single spike on event_date
    'recurring_every_n_years',   -- periodic spike, anchored on event_date, every N years
    'phase_change_start',        -- step function: monthly_delta_usd applies from phase_start_date onward
    'phase_change_end',          -- step function: monthly_delta_usd applies from phase_start_date to phase_end_date
    'none'                       -- no cashflow effect (e.g. promotion w/o income detail, sigma_calibration)
  )),

  -- ONE-SHOT fields.
  one_shot_amount_usd NUMERIC(12,2) NULL,  -- signed: negative = expense, positive = income
  one_shot_date DATE NULL,

  -- RECURRING fields.
  recurring_amount_usd NUMERIC(12,2) NULL,  -- signed
  recurring_period_years INTEGER NULL CHECK (recurring_period_years IS NULL OR recurring_period_years > 0),
  recurring_anchor_date DATE NULL,           -- when the first occurrence happens; subsequent at +N years
  recurring_end_date DATE NULL,              -- optional: hard stop (e.g. college fund 2030-2034)

  -- PHASE-CHANGE fields.
  monthly_delta_usd NUMERIC(12,2) NULL,  -- signed; negative = expense reduction, positive = income or savings
  phase_start_date DATE NULL,
  phase_end_date DATE NULL,              -- NULL = open-ended (kids never come back home)

  -- Backwards-compat / legacy fields (preserved for FK + UI display).
  description TEXT NULL,
  source_id INTEGER NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

  -- Shape-consistency CHECK constraints.
  CHECK (
    (delta_kind = 'one_shot' AND one_shot_amount_usd IS NOT NULL AND one_shot_date IS NOT NULL
       AND recurring_amount_usd IS NULL AND monthly_delta_usd IS NULL)
    OR (delta_kind = 'recurring_every_n_years' AND recurring_amount_usd IS NOT NULL
       AND recurring_period_years IS NOT NULL AND recurring_anchor_date IS NOT NULL
       AND one_shot_amount_usd IS NULL AND monthly_delta_usd IS NULL)
    OR (delta_kind = 'phase_change_start' AND monthly_delta_usd IS NOT NULL
       AND phase_start_date IS NOT NULL AND phase_end_date IS NULL
       AND one_shot_amount_usd IS NULL AND recurring_amount_usd IS NULL)
    OR (delta_kind = 'phase_change_end' AND monthly_delta_usd IS NOT NULL
       AND phase_start_date IS NOT NULL AND phase_end_date IS NOT NULL
       AND phase_end_date > phase_start_date
       AND one_shot_amount_usd IS NULL AND recurring_amount_usd IS NULL)
    OR (delta_kind = 'none')
  )
);

CREATE INDEX ix_life_events_user_category ON life_events (user_id, category);
CREATE INDEX ix_life_events_user_delta_kind ON life_events (user_id, delta_kind);
CREATE INDEX ix_life_events_user_one_shot_date ON life_events (user_id, one_shot_date)
  WHERE one_shot_date IS NOT NULL;
CREATE INDEX ix_life_events_user_phase_start ON life_events (user_id, phase_start_date)
  WHERE phase_start_date IS NOT NULL;
```

**Decisions baked in:**

- `one_shot_date` is NEW (not the old `target_date`). The old `target_date` field is RETIRED — the data migration in §1.5 maps each existing row's `target_date` to whichever of `one_shot_date` / `recurring_anchor_date` / `phase_start_date` is appropriate per its `category`. This is preferable to renaming because (a) the meaning shifts subtly per shape, and (b) it forces every consumer of the old `target_date` field to make an explicit choice at migration time.
- `recurring_amount_usd` + `recurring_period_years` REPLACE the old `recurring_years` + `amount_usd` pair. The old fields are NULLed in the data migration. The old `recurring_years` semantics ("happens every N years for some implicit duration") was ambiguous; the new fields are explicit.
- All amounts are **signed**. Negative = expense (or income reduction). Positive = income (or expense reduction). This is the OPPOSITE of the convention in migration 0042 which forced amount > 0 with the "direction is implicit in the kind" rule. The new convention is cleaner because the cashflow math becomes `expenses_t += -delta_t` uniformly without per-kind sign-flip logic. The old check `amount_usd > 0` is dropped.
- `delta_kind = 'none'` is a legitimate value, not a hack. It exists for events whose category/kind has no cashflow meaning (e.g. `retirement_milestone:sigma_calibration` is a model parameter change, not an expense; `career_event:promotion` without an income detail). These rows still show on the timeline + still trigger replan, but `apply_life_event_deltas()` skips them.

### Section 1.2 — Sign convention table

To make the signed-amount convention unambiguous:

| Scenario | delta_kind | Sign | Meaning |
|---|---|---|---|
| Wedding gift to kid: $50K out | `one_shot` | NEGATIVE | -50000 = $50K expense |
| Inheritance received: $200K | `one_shot` | POSITIVE | +200000 = $200K income |
| RSU vest landing as cash: $100K post-tax | `one_shot` | POSITIVE | +100000 = $100K income |
| New car every 5y: 250k NIS = ~$67K | `recurring_every_n_years` | NEGATIVE | -67000 = $67K expense every 5y |
| Kids leave home: monthly expenses drop $1500 | `phase_change_start` | POSITIVE | +1500 = $1500/mo income (= reduction in expenses) |
| Partner retires: monthly income drops $4000 | `phase_change_start` | NEGATIVE | -4000 = $4000/mo expense (= loss of income) |
| Kid's college: $40K/yr for 4 years (2030-2033) | `recurring_every_n_years` with `recurring_period_years=1, recurring_anchor_date=2030-09-01, recurring_end_date=2034-09-01` | NEGATIVE | -40000 per Sep, 2030-2033 |

The same physical event (e.g. "kids leave home") can usually be modeled multiple ways. The UI nudges toward the simplest shape but doesn't refuse alternatives:

- Kids leave home → `phase_change_start, monthly_delta_usd = +1500` (cleanest).
- Equivalently: a `recurring_every_n_years, period=0 (illegal)` — refused.
- Or: many `one_shot` rows — silly, but legal.

### Section 1.3 — `delta_kind=none` use cases

The following category/kind pairs map to `delta_kind=none` (no cashflow effect):

- `retirement_milestone:sigma_calibration` — model parameter change, not expense.
- `retirement_milestone:withdrawal_policy_change` — policy switch, not expense.
- `career_event:promotion` *when income detail is not provided* — UI offers "did income change? if so, add a phase_change_start; otherwise leave as none".
- `family_event:marriage` (your own) *when no financial detail is provided* — display only.
- `family_event:birth` *when no expense detail is provided* — UI nudges toward phase_change_start at the child's expected start of expensive years, but accepts none.

The point is to keep events on the timeline + in the replan-trigger stream even when they don't shape cashflow. Display ≠ math.

### Section 1.4 — Category × delta_kind interaction matrix

| Category | Allowed delta_kinds | Default delta_kind | UI nudge |
|---|---|---|---|
| career_event | `phase_change_start`, `phase_change_end`, `one_shot`, `none` | `none` | "Did your income change? Pick phase_change_start." |
| family_event | `one_shot`, `phase_change_start`, `phase_change_end`, `none` | `none` | "Big gift? one_shot. Lifestyle shift? phase_change_start." |
| asset_event | `one_shot`, `none` | `one_shot` | "Home purchase / RSU vest / inheritance — one_shot." |
| expense_event | `one_shot`, `phase_change_end` | `one_shot` | "Major medical, college year — one_shot per year or phase_change_end with end date." |
| recurring_expense | `recurring_every_n_years` | `recurring_every_n_years` | "New car / renovation / family travel — pick the period." |
| retirement_milestone | `none`, `phase_change_start` | `none` | "Sigma / annuity / policy decisions — none. Target retire year change — phase_change_start at your new target only if the date matters to other consumers." |

This map drives the UI form's dependent-dropdown behavior (when category changes, the available delta_kinds change) AND drives the server-side validator (a `recurring_expense` with `delta_kind=one_shot` is refused with a structured 422 per §2.3).

### Section 1.5 — Data migration of existing rows

The alembic migration 0049 includes a Python data-migration step that runs AFTER the schema change. For each existing row:

```python
def upgrade():
    # ... DDL above ...
    
    # Data migration step.
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, category, kind, target_date, amount_usd, recurring_years FROM life_events")).fetchall()
    for r in rows:
        category, kind, target_date, amount_usd, recurring_years = r[1], r[2], r[3], r[4], r[5]
        decision = _classify_legacy_row(category, kind, target_date, amount_usd, recurring_years)
        # decision is one of:
        #   ('one_shot', sign, amount, date)
        #   ('recurring_every_n_years', sign, amount, period, anchor_date)
        #   ('phase_change_start', sign, monthly_delta, start_date)
        #   ('none',)
        #   ('none', description_override)  <- e.g. legacy retirement_milestone:target_retire_year_change becomes a none row with target_date preserved in description text
        # Apply the decision via UPDATE / DELETE.
        ...
```

**Codex BLOCKER #1 resolution** (consistent treatment of `retirement_milestone:target_retire_year_change`): the row is CONVERTED to `delta_kind=none`, NOT dropped. The original `target_date` is preserved by serializing it into the `description` field as `"Legacy target retire year: YYYY-MM-DD"` so the user still sees the historical intent on the timeline + recorded-events list. No data is deleted; the cashflow effect is `none` because the new model doesn't honor the date-clamp semantic. §2.5 (clamp-removal regression) and §7.3 (test plan) reference the SAME behavior: post-migration, these rows exist with `delta_kind=none` and have ZERO effect on `effective_retire_ready_age()`.

**Decision table** (executed by `_classify_legacy_row`):

| Legacy (category, kind) | Has target_date? | Has amount_usd? | Has recurring_years? | Decision | Lossy? |
|---|---|---|---|---|---|
| `asset_event:other_asset_acquired` (from RSU pre-vest CTA per sprint #2) | yes | yes | no | `one_shot, +amount, target_date` | No — exact preservation |
| `asset_event:home_purchase` | yes | yes | no | `one_shot, -amount, target_date` | No |
| `asset_event:home_sale` | yes | yes | no | `one_shot, +amount, target_date` | No |
| `asset_event:inheritance` | yes | yes | no | `one_shot, +amount, target_date` | No |
| `expense_event:college` | yes | yes | no | `one_shot, -amount, target_date` + **conversion-assistant prompt on first /life-events page load** (codex IMPORTANT #2) — modal asks "we recorded this as a single college expense; most users pay multi-year. Convert to annual recurring for 4 years?" with Yes/Skip options | **Yes** — original semantics didn't disambiguate one-year vs four-year college; we pick one_shot conservatively, then nudge the user to upgrade via the conversion assistant. The assistant fires only once per migrated row (acknowledged in `life_events_migration_log.user_decision` column). |
| `expense_event:medical_major` | yes | yes | no | `one_shot, -amount, target_date` | No |
| `expense_event:one_time_large` | yes | yes | no | `one_shot, -amount, target_date` | No |
| `recurring_expense:new_car` | optional | yes | yes | `recurring_every_n_years, -amount, recurring_years, target_date OR today` | Slightly — anchor date defaults to TODAY (next occurrence happens now) if target_date is missing. Codex IMPORTANT #1 — `today + period` was wrong, would underestimate near-term expenses. |
| `recurring_expense:major_renovation` | optional | yes | yes | `recurring_every_n_years, -amount, recurring_years, target_date OR today` | Same as above |
| `recurring_expense:family_travel` | optional | yes | yes | `recurring_every_n_years, -amount, recurring_years, target_date OR today` | Same |
| `retirement_milestone:target_retire_year_change` | yes | no | no | `none` (with original target_date preserved in description as "Legacy target retire year: YYYY") | **Yes** — this is the WRONG model; the row is retained for timeline/audit but its cashflow effect is `none` (codex BLOCKER #1 integration — convert, don't drop, so the row remains visible) |
| `retirement_milestone:sigma_calibration` | no | no | no | `none` | No |
| `retirement_milestone:annuity_decision` | no | no | no | `none` | No |
| `retirement_milestone:withdrawal_policy_change` | no | no | no | `none` | No |
| `career_event:*` | optional | no | no | `none` (best-effort; UI will offer to add detail later) | **Yes** — career events lose any implicit income-change signal that was in `description` free text. Warning logged. |
| `family_event:*` | optional | no | no | `none` (best-effort) | **Yes** — same. Warning logged. |
| ANY other unrecognized combination | — | — | — | `none` + warning | Yes |

**Lossy-conversion logging:** each lossy decision writes a row to a new `life_events_migration_log` table:

```sql
CREATE TABLE life_events_migration_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  original_life_event_id INTEGER NOT NULL,
  original_category TEXT NOT NULL,
  original_kind TEXT NOT NULL,
  original_target_date DATE NULL,
  original_amount_usd NUMERIC(12,2) NULL,
  original_recurring_years INTEGER NULL,
  decision TEXT NOT NULL,     -- 'one_shot' | 'recurring_every_n_years' | 'phase_change_start' | 'none' (no 'drop' — codex BLOCKER #1: every row is converted, never dropped)
  reason TEXT NOT NULL,       -- one-line human-readable
  user_decision TEXT NULL,    -- codex IMPORTANT #2: filled when user acts on the conversion assistant ('upgraded_to_recurring' | 'kept_one_shot' | 'edited_manually' | NULL = not yet reviewed)
  user_decision_at DATETIME NULL,
  migrated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

The table is permanent (not dropped after migration) so the user can audit the conversion via a future `/life-events?show=migration_log` query (out of scope for this sprint but the data is preserved).

**User-visible nudge** (codex BLOCKER #2 — non-blocking notification was too weak per [[feedback_ask_dont_assume]]):

Two-tier surfacing:

1. **`/home` Red-Flag Strip info flag** — system-generated `monitor_flags` row of kind `info` inserted IF any rows were converted lossily, with text "Life events were upgraded to the new cashflow model. N rows were converted with partial information loss." Payload references `life_events_migration_log` IDs.

2. **`/life-events` page acknowledgment banner** — at the top of the page, a yellow banner reads "N life events were converted by the new schema. Review and confirm the conversions before adding new events." with a "Review conversions →" CTA opening a modal that lists each `life_events_migration_log` entry with the original shape, the new shape, and an "Edit" link to fix in-place. The banner is gated on a new column `users.life_events_migration_acknowledged_at` (nullable datetime). The banner is NOT dismissable until the user clicks "I've reviewed all conversions" — at which point the column is set + the banner disappears. The CTA explicitly does NOT block the user from creating new events (no modal trap); it just keeps the banner visible until acknowledged.

3. **Add events to the form** — the form remains usable while the banner is visible; the banner sits above the form sections. The user can create new events without dismissing the banner.

`users.life_events_migration_acknowledged_at` is added by migration 0049 as a nullable datetime column. Set to NULL by default; set to NOW() when the user clicks acknowledge. A user with no migration_log rows (fresh DB, never had legacy life events) has the column auto-set to the migration's run timestamp so the banner never appears for them.

Per [[feedback_ask_dont_assume]] this puts the lossy-conversion decision in front of Ariel rather than auto-deciding silently.

### Section 1.6 — `LifeEvent` ORM model

`argosy/state/models.py` updates the `LifeEvent` class to:
- Drop the `target_date`, `amount_usd`, `recurring_years` columns from the ORM (DB columns are also dropped in migration 0049 after data migration completes).
- Add: `delta_kind`, `one_shot_amount_usd`, `one_shot_date`, `recurring_amount_usd`, `recurring_period_years`, `recurring_anchor_date`, `recurring_end_date`, `monthly_delta_usd`, `phase_start_date`, `phase_end_date`.
- Indexes match §1.1.
- Add helper property `cashflow_effective_dates(today, horizon_months) -> list[date]` for the timeline card (returns the actual dates this event impacts cashflow, per-shape).

## Section 2 — Cashflow integration

### Section 2.0 — The sign-flip helper (codex BLOCKER #3)

Sign convention is a footgun. To eliminate the risk of inverted-sign bugs at any layer (validator, recurring expander, monitor diff, etc.), the spec mandates **one and only one** site that flips the signed life-event amount into an expense-series contribution:

```python
def _apply_signed_delta_to_series(
    series: list[float],
    m_offset: int,
    amount_usd_signed: float,
    usd_to_nis: float,
) -> None:
    """The ONLY place in the codebase that converts a signed life-event
    amount into a positive expense-flow contribution.

    Contract:
      positive amount_usd_signed  = income (or expense reduction)
      negative amount_usd_signed  = expense (or income reduction)
      series[m] convention        = positive = expense outflow (NIS)

    Math:
      series[m_offset] += -(amount_usd_signed * usd_to_nis)

      Result:
        amount_usd_signed = +200  (income)  -> series[m] decreases (expense down)
        amount_usd_signed = -200  (expense) -> series[m] increases (expense up)

    This helper is the sole interpreter of the sign convention. NO OTHER
    CALLER in the codebase may perform `-(amount * fx)` arithmetic on a
    life-event amount. apply_life_event_deltas, the timeline-card recurring
    expander, the monitor agent's diff comparator, and the projection
    feedback loop ALL go through this helper.
    """
    series[m_offset] += -(amount_usd_signed * usd_to_nis)
```

**Test matrix** (codex BLOCKER #3 mandate):

| Case | amount_usd_signed | usd_to_nis | Expected series delta |
|---|---|---|---|
| Income, FX>0 | +200 | 3.7 | -740 |
| Expense, FX>0 | -200 | 3.7 | +740 |
| Income, FX=0 (degenerate) | +200 | 0.0 | 0 |
| Expense, FX=0 (degenerate) | -200 | 0.0 | 0 |
| Zero amount | 0 | 3.7 | 0 |
| Income on negative series (already income-spike from another event) | +50 | 3.7 | series[m] -= 185 |
| Expense on positive series | -50 | 3.7 | series[m] += 185 |

All seven cases asserted in `tests/services/test_signed_delta_helper.py`.

### Section 2.1 — The pure function

New in `argosy/services/cashflow_projection.py`:

```python
def apply_life_event_deltas(
    monthly_expense_series: list[float],   # length = horizon_months; NIS
    life_events: list[LifeEvent],
    projection_start_date: date,           # the date that maps to monthly_expense_series[0]
    horizon_months: int,                   # len(monthly_expense_series) must equal this
    usd_to_nis: float,                     # FX at projection time, applied uniformly
) -> list[float]:
    """Modify the projected expense series per life events.

    Returns a new list (length = horizon_months) where:
      - phase_change_start events add monthly_delta_usd × usd_to_nis (sign-flipped:
        the function's contract is positive expense = expense, so a +1500 USD delta
        that means "kids leave home → less expense" is subtracted from the series.
        See §2.2 for the explicit sign math)
      - one_shot events add a spike at one_shot_date's month-offset
      - recurring events add a spike at every (anchor + k*period_years) within horizon
      - delta_kind='none' events are skipped

    The function is PURE: no DB access, no session, no clock reads. All time math
    is anchored to projection_start_date.

    Sign convention:
      INPUT  monthly_expense_series:    positive = expense outflow (NIS)
      INPUT  life_event amounts:        signed (negative = expense, positive = income)
      OUTPUT modified series:            positive = expense outflow (NIS)
      OUTPUT modifier:                   -(amount_usd_signed × usd_to_nis)
                                          (because expense flow = negative cashflow,
                                           so a +1500 USD income event REDUCES the
                                           expense series by 1500 × usd_to_nis)

    Raises:
      ValueError if len(monthly_expense_series) != horizon_months.
      Does NOT raise on events outside the horizon; they're silently skipped.
    """
```

### Section 2.2 — Cashflow math per delta_kind

All sign-flip arithmetic below MUST go through `_apply_signed_delta_to_series` from §2.0. The expanded math is shown for clarity but the implementation calls the helper.

#### One-shot

For each `life_event` with `delta_kind == 'one_shot'`:

```
m_offset = months_between(projection_start_date, one_shot_date)
if 0 <= m_offset < horizon_months:
    _apply_signed_delta_to_series(series, m_offset, one_shot_amount_usd, usd_to_nis)
    # Equivalent expansion:
    # series[m_offset] += -(one_shot_amount_usd × usd_to_nis)
```

**Boundary rule:** the month containing `one_shot_date` is INCLUDED. A spike on `2030-01-15` lands in series index for `2030-01`. A spike on `2030-01-01` also lands in `2030-01`. The `months_between` helper rounds DOWN to the start of the month (`months_between(2026-05-29, 2030-01-15) = (2030-01 - 2026-05) = 44`).

**Events at exactly `projection_start_date`:** `m_offset = 0`, applied to series[0]. NOT excluded.

**Events before `projection_start_date`:** `m_offset < 0`, skipped (already happened).

**Events at exactly the horizon edge** (`m_offset == horizon_months`): skipped (one past the end).

#### Recurring

For each `life_event` with `delta_kind == 'recurring_every_n_years'`:

```
period_months = recurring_period_years × 12
first_offset = months_between(projection_start_date, recurring_anchor_date)
end_offset = (
    months_between(projection_start_date, recurring_end_date)
    if recurring_end_date is not None else horizon_months
)
k = 0
while True:
    m_offset = first_offset + k × period_months
    if m_offset >= min(horizon_months, end_offset):
        break
    if m_offset >= 0:
        _apply_signed_delta_to_series(series, m_offset, recurring_amount_usd, usd_to_nis)
        # Equivalent: series[m_offset] += -(recurring_amount_usd × usd_to_nis)
    k += 1
```

**Anchor-date offset preservation:** the first occurrence lands at the anchor's month, NOT at the start of the period. Example: car bought 2027-Mar, period=5y → recurrences at 2027-Mar, 2032-Mar, 2037-Mar — NOT 2032-Jan or 2027-Jan.

**Period=1** is legal and means annual (e.g. kid's college). Period=0 is illegal (CHECK constraint at DB level + Pydantic).

**Anchor before projection_start_date:** the loop starts at `k=0` with `m_offset = first_offset < 0`, skips that occurrence, increments k, and lands the first valid recurrence inside the horizon. Example: car bought 2024-Mar (before 2026-05-29 projection_start), period=5y → first recurrence inside horizon at 2029-Mar (k=1). This handles "car bought 3 years ago — next one in 2 years" correctly without special-casing.

**End date semantics:** `recurring_end_date` is EXCLUSIVE. A college fund with `anchor=2030-09-01, period=1, end_date=2034-09-01` fires at 2030-Sep, 2031-Sep, 2032-Sep, 2033-Sep — four occurrences. The 2034-Sep occurrence is excluded.

#### Phase change

For each `life_event` with `delta_kind == 'phase_change_start'` or `'phase_change_end'`:

```
start_offset = months_between(projection_start_date, phase_start_date)
end_offset = (
    months_between(projection_start_date, phase_end_date)
    if delta_kind == 'phase_change_end' and phase_end_date is not None
    else horizon_months
)
for m in range(max(0, start_offset), min(horizon_months, end_offset)):
    _apply_signed_delta_to_series(series, m, monthly_delta_usd, usd_to_nis)
    # Equivalent: series[m] += -(monthly_delta_usd × usd_to_nis)
```

**Step-function boundary:** the month containing `phase_start_date` IS INCLUDED. A "kids leave home" phase starting on `2034-08-15` means August 2034 already has the new (lower) expense level. This is a step function, not a ramp.

**Phase-change before projection_start_date:** `start_offset < 0`, treated as `start_offset = 0` — the phase is already active at projection start. Example: "kids left home in 2025" applied to a 2026-05-29 projection: every month from index 0 has the reduced expense level.

**Phase-change extending past horizon:** `end_offset > horizon_months`, capped to `horizon_months`. The full horizon shows the new level.

### Section 2.3 — `project_cashflow()` integration

Inside `project_cashflow()`, the existing inflation loop at lines 480-500 computes `expenses_t` per month. The integration:

```python
# Step 1: build the un-modified inflated expense series (existing logic).
inflated_series = [
    inflate_expenses(
        household.monthly_expenses_nis,
        effective_expense_growth, t,
    )
    for t in range(years * 12)
]

# Step 2: apply life-event deltas as expense-series modifiers.
life_events = _load_life_events_for_projection(session, user_id)
usd_to_nis = _current_usd_to_nis()    # uses existing FX lookup; see §2.4
modified_series = apply_life_event_deltas(
    monthly_expense_series=inflated_series,
    life_events=life_events,
    projection_start_date=today,
    horizon_months=years * 12,
    usd_to_nis=usd_to_nis,
)

# Step 3: existing per-month loop reads from modified_series instead of
# recomputing expenses_t inline.
for t, expenses_t in enumerate(modified_series):
    ...  # existing surplus / portfolio math
```

**Why life events are applied AFTER inflation, not before:** life events are denominated in today's dollars (USD), explicitly. A wedding gift of $50K in 2034 means $50K-in-2034 purchasing power, not "$50K-in-2026 inflated to 2034." This matches the user's mental model: when he says "kids leave home → -$1500/month", he means "$1500 less per month in then-current dollars at the time the kids leave." If a future version wants to inflation-adjust the deltas, that's a per-event opt-in flag, not a default. Documented in the docstring + worked example below.

(Codex BLOCKER risk surface — see codex review focus appendix §6.)

### Section 2.4 — FX lookup

`_current_usd_to_nis()` reads the same FX value used elsewhere in `cashflow_projection.py`. Today there's a constant default; the future regime-switching FX module from sprint #1 will replace it with a scenario-dependent value. This spec USES whatever the current `cashflow_projection.py` uses — does not introduce a new FX path. If the FX lookup is async-only at the time of this commit, the implementer wraps it in the same sync-call adapter the rest of the file uses.

**FX v1 decision (codex IMPORTANT #4 — locked):** single base-scenario FX in v1. Rationale: the FX variability across bear/base/bull scenarios in the existing `cashflow_projection.py` is typically <10% over a 30-year horizon (the FX regime model treats USD/NIS as mean-reverting). The dominant uncertainty on life-event amounts is the user's own estimate (a "$50K wedding" could realistically be $30K-$80K — a 60% range). Applying three FX values produces three series that differ by <10% at the life-event component, well below the noise floor of the amount itself. A scenario-keyed FX adds signature complexity without materially improving correctness.

If a future revision makes the FX regime more variable (e.g. a tail-risk scenario where USD/NIS doubles), the signature is promoted to `usd_to_nis_by_scenario: dict[str, float]` and `apply_life_event_deltas` invoked three times. Not for v1.

The single FX value used is the `usd_to_nis` constant from the existing `cashflow_projection.py` (matches whatever the rest of the engine uses — no new lookup path introduced).

### Section 2.5 — Removal of the life-event clamp branch

`effective_retire_ready_age()` currently has (lines 879-986):

```python
# Clamp 3: life event (stub for now).
life_clamp_date = None

candidates = [
    ("base", base_retire_date, "no_clamp_needed"),
    ("rsu", rsu_clamp_date, "rsu_unvested"),
    ("life", life_clamp_date, "life_event"),
]
```

This is conceptually wrong — the life event's effect on retirement-readiness must flow through cashflow, not through a date clamp. Commit #3 removes the `life_clamp_date` variable AND removes the `("life", life_clamp_date, "life_event")` candidate AND removes the `'life_event'` value from `clamp_reason` literals AND removes `life_event_clamp_date` from the `EffectiveRetireReadyAge` dataclass.

**Replacement comment** at the removal site:

```python
# Life-event clamp REMOVED in commit #3 of the /life-events redesign
# (spec docs/superpowers/specs/2026-05-29-life-events-cashflow-redesign-design.md).
# Reason: a life event's effect on retire-readiness flows through the
# cashflow projection (apply_life_event_deltas modifies monthly_expense_series,
# which propagates through surplus calculation, which determines the solvency
# crossing). A second date-clamp pathway double-counts.
# RSU clamp survives because RSUs are a real liquidity constraint
# (you can't sell unvested shares), not a cashflow-shape modifier.
```

**Test surface impact:** any test asserting `clamp_reason == 'life_event'` is rewritten to assert the retire-ready age moved (or didn't) via the cashflow path. Specifically: a test that creates a `retirement_milestone:target_retire_year_change` row with a future date AND asserts retire-ready clamps — that test is REWRITTEN to assert the row exists with `delta_kind=none` and has no effect on retire-ready age (the row is preserved per codex BLOCKER #1, but its effect is now nil). Existing passing tests for the RSU clamp continue to pass because the RSU clamp is untouched.

**Per-file consumer checklist** (codex BLOCKER #4 — explicit list of files touched by the clamp removal):

| File | Required edits | Notes |
|---|---|---|
| `argosy/services/cashflow_projection.py` | Remove `life_clamp_date` variable; remove the `("life", life_clamp_date, "life_event")` candidate tuple; remove `life_event_clamp_date: date \| None` from `EffectiveRetireReadyAge` dataclass; remove `'life_event'` from `clamp_reason` literal type; update the dataclass docstring | Lines 879-986 + surrounding |
| `argosy/services/retirement_timeline.py` | Update `_load_life_events` to filter by the new shape columns (replace `target_date IS NOT NULL` with delta_kind-aware filter from §5); update `LifeEventMarker` dataclass per §5 | The `effective_retire_ready_age()` consumer at line 326 in this file continues to work — the function's return type loses a field but the consumer doesn't read it |
| `argosy/services/retirement/replan_triggers.py` | Update the `life_event` trigger description string per §2.6 — no logic change | Description-only |
| `argosy/api/routes/retirement.py` (any RetireReadyAge serialization endpoint) | If the response model includes `life_event_clamp_date`, remove the field from the response schema | grep — flag if no consumer found |
| `ui/src/components/retirement/*.tsx` (RetirementAgeCard, RuinProbabilityHero) | If any rendering branch reads `clamp_reason === 'life_event'`, delete that branch | grep before commit |
| `tests/services/test_cashflow_projection.py` (and related) | Update + remove tests per §7.3 | Concrete count provided in test file |
| `tests/services/test_effective_retire_ready_age_*.py` | Update + remove tests asserting `clamp_reason == 'life_event'` | All such tests reroute through the cashflow path |

The codex review explicitly asked for this checklist (BLOCKER #4) so the blast radius is enumerated upfront, not discovered piecemeal during commit #3.

### Section 2.6 — `replan_triggers.life_event` semantics update

The `life_event` trigger kind in `argosy/services/retirement/replan_triggers.py` STAYS, but its semantic shifts:

- **Before:** "user added a life event → maybe the retire date is clamped → replan."
- **After:** "user added a life event → the projected expense series changed → replan."

No code change needed in `replan_triggers.py`. The trigger still fires; what fires it is still `LifeEventCreate`. The description string in `list_known_triggers()` is updated:

```python
{"kind": "life_event",
 "description": "Life event added/edited — cashflow series shape changed; "
                "projection should re-compose."},
```

The HolisticTimelineCard, the monitor agent's drift comparison, the Monte Carlo, etc. all consume the new (modified) expense series transparently — they don't need to know a life event exists, only that the projection numbers shifted.

## Section 3 — API + service layer

### Section 3.1 — New Pydantic schema with discriminated union

The current `LifeEventCreateRequest` has `category` (enum), `kind` (string, per-category-validated), plus untyped `target_date` / `amount_usd` / `recurring_years`. The new schema uses a Pydantic discriminated union on `delta_kind`:

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field, Tag, discriminator

class LifeEventBase(BaseModel):
    user_id: str
    category: LifeEventCategory
    kind: str
    description: str | None = None
    source_id: int | None = None

class OneShotPayload(LifeEventBase):
    delta_kind: Literal['one_shot'] = 'one_shot'
    one_shot_amount_usd: float   # signed
    one_shot_date: date

class RecurringPayload(LifeEventBase):
    delta_kind: Literal['recurring_every_n_years'] = 'recurring_every_n_years'
    recurring_amount_usd: float   # signed
    recurring_period_years: Annotated[int, Field(gt=0)]
    recurring_anchor_date: date
    recurring_end_date: date | None = None

class PhaseChangeStartPayload(LifeEventBase):
    delta_kind: Literal['phase_change_start'] = 'phase_change_start'
    monthly_delta_usd: float   # signed
    phase_start_date: date

class PhaseChangeEndPayload(LifeEventBase):
    delta_kind: Literal['phase_change_end'] = 'phase_change_end'
    monthly_delta_usd: float   # signed
    phase_start_date: date
    phase_end_date: date         # @model_validator: > phase_start_date

class NonePayload(LifeEventBase):
    delta_kind: Literal['none'] = 'none'

LifeEventCreateRequest = Annotated[
    Union[OneShotPayload, RecurringPayload, PhaseChangeStartPayload, PhaseChangeEndPayload, NonePayload],
    Field(discriminator='delta_kind'),
]
```

Pydantic's discriminator field validation rejects payloads where the `delta_kind` doesn't match the supplied fields. The route still catches `InvalidKindForCategoryError` and returns the structured 422 banner contract from sprint #1 §4.1.

**New error class:** `InvalidDeltaKindForCategoryError` — same shape as `InvalidKindForCategoryError`, raised when the category/delta_kind interaction matrix from §1.4 is violated.

### Section 3.2 — `field_rules_by_category` catalog rewrite

The current catalog returns:

```python
field_rules_by_category[category] = {
    "requires_amount": bool,
    "supports_recurring_years": bool,
}
```

After this sprint:

```python
field_rules_by_category[category] = {
    "allowed_delta_kinds": list[str],         # from §1.4 table
    "default_delta_kind": str,                # the UI's initial pick
    "nudge": str,                             # human-readable per-category hint
}
```

The UI consumes this map to drive the dependent-dropdown behavior (when category changes, the available delta_kinds change).

**Per-delta_kind required-fields map** (new):

```python
required_fields_by_delta_kind = {
    "one_shot": ["one_shot_amount_usd", "one_shot_date"],
    "recurring_every_n_years": ["recurring_amount_usd", "recurring_period_years", "recurring_anchor_date"],
    "phase_change_start": ["monthly_delta_usd", "phase_start_date"],
    "phase_change_end": ["monthly_delta_usd", "phase_start_date", "phase_end_date"],
    "none": [],
}
```

Also surfaced from the catalog endpoint so the UI can validate before submit.

### Section 3.3 — Routes

The `POST /api/life-events`, `PUT /api/life-events/{id}`, `DELETE /api/life-events/{id}`, `GET /api/life-events?user_id=` routes stay structurally identical. Payload schemas change per §3.1. The catalog endpoint `GET /api/life-events/catalog` returns the updated shape per §3.2.

422 contract preserved verbatim:

```json
{
  "error": "kind_not_valid_for_category",
  "input": "...",
  "valid_categories": [...],
  "valid_kinds": [...],
}
```

New 422 variant:

```json
{
  "error": "delta_kind_not_valid_for_category",
  "category": "recurring_expense",
  "delta_kind": "one_shot",
  "allowed_delta_kinds": ["recurring_every_n_years"],
}
```

The UI extends its existing red-banner handler to recognize the new variant.

## Section 4 — UI form rewrite

### Section 4.1 — Page structure

`ui/src/app/life-events/page.tsx` is reorganized into three labeled sections:

```
┌──────────────────────────────────────────────┐
│ Life Events                                  │
│ Record how your cashflow changes across      │
│ phases of life. The plan re-composes from    │
│ the new shape.                               │
│                                              │
│ ──── One-shot expenses or income ───────────│
│   Category: [asset_event ▾]                 │
│   Kind:     [other_asset_acquired ▾]        │
│   Date:     [2030-09-01]                    │
│   Amount:   [+50000] USD  (signed)          │
│   Note:     "Inheritance from..."           │
│                              [Save]         │
│                                              │
│ ──── Recurring expenses ────────────────────│
│   Category: [recurring_expense ▾]           │
│   Kind:     [new_car ▾]                     │
│   Amount:   [-67000] USD per occurrence     │
│   Period:   [5] years                        │
│   First on: [2027-03-15]                    │
│   Until:    [    ] (optional)               │
│                              [Save]         │
│                                              │
│ ──── Phase changes (when life patterns shift)│
│   Category: [family_event ▾]                │
│   Kind:     [dependent_leaves ▾]            │
│   Monthly delta: [+1500] USD (signed)        │
│   Starts:   [2034-08-15]                    │
│   Ends:     [    ] (optional, leave blank   │
│                    for open-ended)          │
│                              [Save]         │
│                                              │
│ ──── Recorded life events ──────────────────│
│   [list, shape-aware rendering...]          │
└──────────────────────────────────────────────┘
```

Each section is a self-contained form. The category dropdown is constrained per section (one_shot section shows only categories whose `allowed_delta_kinds` includes `one_shot`, etc.). Visually, this trades "one wizard for everything" for "three explicit shape pickers" — which matches the conceptual model ("which SHAPE is this event?").

**Alternative considered + rejected:** a single form with delta_kind as the first dropdown and per-shape conditional fields below. This matches the data model 1:1 but doesn't reinforce the "cashflow phase modeler" mental shift — a user picking `delta_kind=one_shot` from a generic dropdown doesn't internalize the framing the way "this goes in the One-shot section" does. The user explicitly flagged the framing as the most important part of the redesign.

### Section 4.2 — Server-driven dependent dropdowns

When category changes, the UI:
1. Reads `kinds_by_category[new_category]` for the kind dropdown.
2. Reads `field_rules_by_category[new_category].allowed_delta_kinds` to validate the SECTION the user is in. If the picked category isn't valid for this section, show inline error "[Category] doesn't support [shape]. Move to the [other] section." with a deep link.

### Section 4.3 — `<UpcomingVestCard>` prefill contract update

`ui/src/components/retirement/UpcomingVestCard.tsx::buildLifeEventHref()` currently emits:

```
/life-events?prefill_category=asset_event&prefill_kind=other_asset_acquired
            &prefill_date=2026-09-15&prefill_amount=120000
            &prefill_description=RSU%20vest%20from%20grant%20ABC
```

After this sprint, the same CTA emits:

```
/life-events?section=one_shot
            &prefill_category=asset_event
            &prefill_kind=other_asset_acquired
            &prefill_delta_kind=one_shot
            &prefill_one_shot_date=2026-09-15
            &prefill_one_shot_amount_usd=120000  (positive — income)
            &prefill_description=RSU%20vest%20from%20grant%20ABC
```

`section` query param scrolls the page to the matching section + focuses its first field. The page's `formFromPrefill()` initializer reads the new keys + maps them onto the appropriate section's state.

**UpcomingVestCard prefill guard (codex IMPORTANT #5 — defensive):** the existing prefill helper at `ui/src/components/retirement/UpcomingVestCard.tsx::buildLifeEventHref` uses `Math.round(vest.expected_post_tax_nominal_usd)` directly. The new contract requires `one_shot_amount_usd = +X` for income; if the upstream calculation ever returns a negative or NaN value (e.g. tax rate >100% bug, missing FMV), the prefill would silently produce a wrong-direction event. Commit #5 adds an explicit guard:

```typescript
function buildLifeEventHref(vest: UpcomingVestDTO): string {
  const raw = vest.expected_post_tax_nominal_usd;
  if (!Number.isFinite(raw) || raw <= 0) {
    // Defensive: if the post-tax estimate is broken upstream, don't
    // generate a malformed prefill. The CTA renders disabled.
    return "/life-events?section=one_shot";
  }
  const amount = Math.round(raw);
  const params = new URLSearchParams({
    section: "one_shot",
    prefill_category: "asset_event",
    prefill_kind: "other_asset_acquired",
    prefill_delta_kind: "one_shot",
    prefill_one_shot_date: vest.expected_vest_date,
    prefill_one_shot_amount_usd: String(amount),  // always positive
    prefill_description: `RSU vest from grant ${vest.grant_id}`,
  });
  return `/life-events?${params.toString()}`;
}
```

The `<UpcomingVestCard>` renders the "Add as life event →" link as disabled when the helper returns the bare `/life-events?section=one_shot` URL (the disabled state shows tooltip "Estimate unavailable").

A round-trip unit test in `ui/__tests__/upcoming-vest-card-prefill.spec.tsx` asserts: (a) a normal positive post-tax amount produces a URL with `prefill_one_shot_amount_usd=<positive>`; (b) `expected_post_tax_nominal_usd = -100` produces the disabled-link bare URL; (c) `expected_post_tax_nominal_usd = NaN` produces the disabled-link bare URL.

### Section 4.4 — Recorded-events list rendering

`<EventRow>` is updated to render per-shape:

- **One-shot:** `[asset_event] [one_shot] 2030-09-01 +$50,000 "Inheritance"`
- **Recurring:** `[recurring_expense] [new_car] every 5y, -$67,000, first 2027-03-15 "..."`
- **Phase-change-start:** `[family_event] [dependent_leaves] from August 2034 onward (open-ended), +$1,500/mo "..."` — codex IMPORTANT #3: the "from <Month YYYY> onward" copy makes the inclusive-month-of-start semantic explicit to the user; the day-of-month inside that start month is not displayed because the math rounds to the start month.
- **Phase-change-end:** `[expense_event] [college] September 2030 → September 2034, -$3,333/mo "..."` — same convention for start and end (end is exclusive — September 2034 is NOT included).
- **None:** `[retirement_milestone] [sigma_calibration] no cashflow effect "..."`

Sort: by the FIRST cashflow-effective date (one_shot_date / recurring_anchor_date / phase_start_date) ascending, with `none` rows last.

## Section 5 — `<HolisticTimelineCard>` updates

`argosy/services/retirement_timeline.py::LifeEventMarker` currently has:

```python
@dataclass(frozen=True)
class LifeEventMarker:
    date: date
    category: str
    kind: str
    amount_usd: float | None
    description: str | None
```

`_load_life_events()` filters to `target_date IS NOT NULL`. Both need updates.

**New marker shape:**

```python
@dataclass(frozen=True)
class LifeEventMarker:
    category: str
    kind: str
    delta_kind: str
    description: str | None
    # Shape-specific render fields (only one set is populated per delta_kind).
    one_shot_date: date | None
    one_shot_amount_usd: float | None
    recurring_dates: list[date] | None      # all in-horizon occurrences, computed by service
    recurring_amount_usd: float | None
    phase_start_date: date | None
    phase_end_date: date | None             # None = open-ended
    monthly_delta_usd: float | None
```

**Service-layer expansion:** `_load_life_events()` now expands recurring events into the list of in-horizon occurrence dates, and resolves phase-change date ranges. The expansion uses `LifeEvent.cashflow_effective_dates(today, horizon_months)` from §1.6.

**`<HolisticTimelineCard>` UI rendering** (markers layer):

- **One-shot** → single dot at the date.
- **Recurring** → N dots at each occurrence, connected by a faint dashed line.
- **Phase-change-start** (open-ended) → vertical line at start_date with arrow extending right.
- **Phase-change-end** → vertical line at start_date + vertical line at end_date + shaded band between them.
- **None** → small grey dot (still rendered for visibility, no math impact).

`delta_kind=none` events with no date at all (e.g. sigma_calibration with no target_date) get a `created_at`-anchored marker labeled "no scheduled date" — they're recorded but not on the timeline. The card pulls them into a sidebar "Undated events" list.

## Section 6 — User-guide refresh

Single commit at the end of the sprint. Sections to refresh:

- `/life-events` section — rewrite to the cashflow-phase-modeler framing. Use the user's "I retire from work not life" quote in the section opener.
- `/retirement` section — clarify that life events affect cashflow, which then determines retire-readiness, which uses the unchanged RSU clamp.
- Replace any text claiming "life events clamp the retirement date" with the new model. Specifically: per [[feedback_user_guide_is_manual]] the user-guide is a manual, never history — so no "we used to model this as a clamp" prose. Just the current behavior.

## Section 7 — Test plan

### Section 7.1 — Pure-function tests (`apply_life_event_deltas`)

In `tests/services/test_cashflow_life_event_deltas.py`:

| # | Scenario | Assertion |
|---|---|---|
| 1 | Empty events list | Output == input |
| 2 | One-shot at offset 12 | series[12] decreased by `amount × fx` (or increased if positive) |
| 3 | One-shot before projection start | Output == input (skipped) |
| 4 | One-shot at exact projection_start_date | series[0] modified |
| 5 | One-shot at exact horizon edge (m_offset == horizon_months) | Output == input |
| 6 | One-shot at month boundary (date = first of month) | series[that month] modified |
| 7 | One-shot at month boundary (date = last day) | Same month modified |
| 8 | Recurring period=1y from anchor | N occurrences at anchor, anchor+12, anchor+24 |
| 9 | Recurring period=5y, anchor 3y BEFORE projection start | First in-horizon occurrence at +2y, then +7y, +12y |
| 10 | Recurring with end_date | No occurrence at or after end_date |
| 11 | Recurring period=1, end_date exactly at first occurrence | Zero occurrences (end_date exclusive) |
| 12 | Phase change start before projection | Whole series modified from m=0 |
| 13 | Phase change end (closed band) | Series modified inside band only |
| 14 | Phase change extending past horizon | Capped at horizon_months |
| 15 | Multiple overlapping phase changes | Additive |
| 16 | One-shot + phase-change same month | Additive |
| 17 | Phase change at exact start_date (month boundary) | First-included month is the start_date's month |
| 18 | Sign: positive amount on one-shot REDUCES expense (income) | series[m] < input[m] |
| 19 | Sign: negative amount on phase-change INCREASES expense | series[m] > input[m] across the phase |
| 20 | `delta_kind=none` events skipped | Output == input even with rows present |
| 21 | FX of 0 (degenerate) | Output == input (sanity, doesn't divide-by-zero) |
| 22 | len(input) != horizon_months | ValueError raised |

### Section 7.2 — Integration test (cashflow projection)

In `tests/services/test_cashflow_projection_with_life_events.py`:

- Create a user with `monthly_expenses_nis=20000`, project 30y.
- Add a phase_change_start at year 8 (kids leave home, +1500 USD/mo).
- Assert: `retire_ready_age_base` is EARLIER than without the phase change (lower forward expenses → earlier solvency).
- Add a one_shot at year 10 (-50000 USD wedding gift).
- Assert: small backward shift in retire_ready_age_base (slightly higher cumulative expenses by year 10).
- Add a recurring (-67000 USD car every 5y starting year 1).
- Assert: significant backward shift; possibly no-crossing-in-horizon if the load is heavy enough.

### Section 7.3 — Removed-clamp regression tests

In `tests/services/test_effective_retire_ready_age_life_event_clamp_removed.py`:

- Create a `retirement_milestone:target_retire_year_change` row with target_date 5y in the future. Run migration 0049. Assert: row STILL EXISTS but with `delta_kind=none`, `description` containing "Legacy target retire year: ..." (codex BLOCKER #1 — converted, not dropped). Assert: retire-ready age computed purely from cashflow + RSU clamp, no `clamp_reason == 'life_event'` ever returned.
- Create the row in the new schema as `delta_kind=none` directly. Assert: retire-ready age computed identically to having no row at all.
- Assert: `EffectiveRetireReadyAge.life_event_clamp_date` field removed (test verifies attribute does not exist).

### Section 7.4 — Data migration tests

In `tests/migrations/test_0049_life_events_cashflow_shape.py`:

- Seed v0042 schema with each row type from the table in §1.5.
- Run migration 0049.
- Assert each row's destination per the decision table.
- Assert lossy conversions logged in `life_events_migration_log`.
- Assert `monitor_flags` row inserted IFF any lossy conversion occurred.
- Test downgrade (best-effort: amounts are signed in new schema, unsigned in old — downgrade picks `abs(amount)` and logs that the sign is lost).

### Section 7.5 — UI tests

In `ui/__tests__/life-events-form.spec.tsx`:

- Render the page in each section. Verify category dropdown is constrained per section.
- Submit one_shot with negative amount → 201, persisted.
- Submit recurring with period=0 → 422 (Pydantic CHECK).
- Submit phase_change_end with end_date <= start_date → 422.
- 422 banner contract preserved per sprint #1 §4.1.
- `<UpcomingVestCard>` "Add as life event →" link includes `section=one_shot` + `prefill_delta_kind=one_shot`. Click navigates + prefills the one-shot section.

### Section 7.6 — `delta_kind='none'` cross-path tests (codex IMPORTANT #6)

In `tests/services/test_life_events_none_kind.py` — explicit assertions that a `none`-kind event does NOT silently behave like any other shape:

| # | Path | Assertion |
|---|---|---|
| 1 | `apply_life_event_deltas` | Series is byte-identical to input when all events are `none` |
| 2 | `apply_life_event_deltas` mixed | One `none` + one `one_shot`: result is same as just the `one_shot` (none contributes zero) |
| 3 | Timeline card `_load_life_events` | `none` events with `created_at` but no other date appear in the "Undated events" sidebar list, NOT on the timeline rendering plane |
| 4 | Timeline card recurring expander | `none` events have no `recurring_dates` field populated (None, not empty list) |
| 5 | Replan trigger | Creating a `none` event still fires the `life_event` replan trigger (it changes the user's recorded state, even if it's display-only) |
| 6 | UI event row | `none` events render with copy "no cashflow effect" and do NOT show amount/date fields that other shapes show |
| 7 | API DTO serialization | `none` events serialize with `delta_kind='none'` and all per-shape fields (`one_shot_*`, `recurring_*`, `phase_*`) as `null` |
| 8 | Migration legacy → none | Post-migration, a legacy `retirement_milestone:target_retire_year_change` is in the new schema with `delta_kind='none'` AND its `description` includes "Legacy target retire year: ..." |

### Section 7.7 — Timeline card tests

In `tests/services/test_retirement_timeline_life_events.py`:

- Recurring event with 3 occurrences in horizon → marker carries 3 dates.
- Phase-change open-ended → marker has end_date=None, UI renders arrow.
- `delta_kind=none` event with no date → marker in sidebar list, not on timeline.

## Section 8 — Risk register

| Risk | Mitigation |
|---|---|
| Existing rows get silently lossy-migrated and user doesn't notice | `life_events_migration_log` table + auto-generated `monitor_flags` info row surfacing on /home Red-Flag Strip after migration. §1.5. |
| Sign-convention flipping breaks UpcomingVestCard prefill (CTA sends positive value into a field that now means "negative is expense") | Per §4.3, the asset_event:other_asset_acquired prefill explicitly uses `prefill_one_shot_amount_usd = +X` for income. Unit test in §7.5 asserts a click-through round-trip. |
| `apply_life_event_deltas` math has an off-by-one at month boundary | Tests #4, #5, #6, #11, #17 in §7.1 pin the boundaries explicitly. |
| Recurring events with anchor BEFORE projection start skip the first valid occurrence | Test #9 in §7.1 verifies anchor=3y-before → first in-horizon at +2y, NOT +5y from anchor. |
| FX scenario-dependence (bear / base / bull use different USD/NIS) is ignored | §2.4: single FX in v1; codex review may push to scenario-keyed FX. Flagged as open question. |
| Removing the life-event clamp breaks consumers we haven't catalogued | §2.5 lists EffectiveRetireReadyAge consumers. Test §7.3 asserts no `clamp_reason == 'life_event'` returned. grep on `life_event_clamp_date` to confirm. |
| Phase-change overlap with itself (user adds two `dependent_leaves` rows for two kids) | Additive math is the desired behavior — two kids leaving at different times = two phase changes = compounding expense reductions. Documented in §2.2; test #15 verifies. |
| Recurring event with anchor IN the future relative to today, recurring_end_date passed silently | The anchor itself counts as occurrence k=0 — if end_date is exactly anchor + 0, zero occurrences fire. CHECK constraint refuses end_date <= start for phase_change_end; for recurring we DON'T constrain because anchor + end at same date = 0 occurrences is a valid (if pointless) configuration. |

## Section 9 — Open dependencies for Ariel

1. **FX scenario-dependence decision** (§2.4): single-FX v1 or scenario-keyed FX? Defers a small amount of code in commit #2. If codex flags as BLOCKER, gets bumped into v1.
2. **Lossy-conversion review** (§1.5): the `_classify_legacy_row` decision table is best-effort heuristic. Once the migration script is written, dump the actual conversion log from Ariel's current DB before applying — Ariel reviews → approves the lossy hits before commit lands. Per [[feedback_ask_dont_assume]].
3. **Three-section UI vs single-form UI** (§4.1): I picked three sections to reinforce the framing. If Ariel prefers a single form, swap before commit #5. Cosmetic only; data model is unchanged.

## Schema appendix (full DDL)

See §1.1 for the final `life_events` table DDL. The migration also adds `life_events_migration_log` per §1.5. No other tables touched.

**Final ORM model summary** (matches the DDL):

```
LifeEvent
  id                       int PK
  user_id                  str FK users(id) CASCADE
  category                 str CHECK 6-value enum
  kind                     str (per-category enum, Pydantic-enforced)
  delta_kind               str CHECK 5-value enum  -- NEW
  one_shot_amount_usd      float?    -- NEW (signed)
  one_shot_date            date?     -- NEW
  recurring_amount_usd     float?    -- NEW (signed)
  recurring_period_years   int? > 0  -- NEW
  recurring_anchor_date    date?     -- NEW
  recurring_end_date       date?     -- NEW
  monthly_delta_usd        float?    -- NEW (signed)
  phase_start_date         date?     -- NEW
  phase_end_date           date?     -- NEW
  description              str?
  source_id                int?
  created_at               datetime
  updated_at               datetime

  -- RETIRED columns (dropped post-migration):
  -- target_date              date?
  -- amount_usd               float?
  -- recurring_years          int?
```

## Cashflow math appendix (worked examples)

Three end-to-end worked examples to make the math concrete. All assume `projection_start_date = 2026-05-29`, `usd_to_nis = 3.70`, `horizon_months = 360` (30 years), and `monthly_expenses_nis = 30000` flat (no inflation in these examples, for clarity — the real engine inflates).

### Example A — "Kids leave home in 2034"

Input event:
```
delta_kind = 'phase_change_start'
monthly_delta_usd = +1500   (positive = expense reduction)
phase_start_date = 2034-08-15
phase_end_date = None
```

Months from projection_start to phase_start_date:
```
(2034-08) - (2026-05) = 8 years 3 months = 99 months
```

So `start_offset = 99`. Loop:
```
for m in range(99, 360):
    series[m] += -(+1500 × 3.70)
            = -5550
```

Result:
- series[0..98]   = 30000 (unchanged)
- series[99..359] = 30000 - 5550 = 24450

Expense series dropped by 5550 NIS/mo from month 99 onward. The projection's solvency crossing happens earlier as a result → `retire_ready_age_base` decreases.

### Example B — "New car every 5y, first in 2027-Mar"

Input event:
```
delta_kind = 'recurring_every_n_years'
recurring_amount_usd = -67000   (negative = expense, ~250k NIS)
recurring_period_years = 5
recurring_anchor_date = 2027-03-15
recurring_end_date = None
```

Months from projection_start to anchor:
```
(2027-03) - (2026-05) = 10 months
```

So `first_offset = 10`, `period_months = 60`. Occurrences (k=0,1,2,...) until `m_offset >= 360`:
```
k=0: m=10  → series[10]  += -(-67000 × 3.70) = +247900 (wait — sign check)
```

Sign check: `series[m] += -(recurring_amount_usd × fx)` with `recurring_amount_usd = -67000`:
```
series[m] += -((-67000) × 3.70)
         = -(-247900)
         = +247900
```

That makes `series[10] = 30000 + 247900 = 277900` NIS for that one month. Correct: a $67K car purchase shows up as +247900 NIS expense spike.

Occurrences:
```
k=0: m=10  (2027-03)  series[10]  += 247900
k=1: m=70  (2032-03)  series[70]  += 247900
k=2: m=130 (2037-03)  series[130] += 247900
k=3: m=190 (2042-03)  series[190] += 247900
k=4: m=250 (2047-03)  series[250] += 247900
k=5: m=310 (2052-03)  series[310] += 247900
k=6: m=370 → break (>= 360)
```

Result: six spike months over 30 years. Anchor month is preserved (every March), period is preserved (5y), occurrences past horizon are skipped.

### Example C — "Wedding gift to kid in 2031: $50K out" + "Inheritance in 2028: $200K in"

Two one-shot events:
```
event 1: delta_kind=one_shot, one_shot_amount_usd=-50000, one_shot_date=2031-06-10
event 2: delta_kind=one_shot, one_shot_amount_usd=+200000, one_shot_date=2028-11-20
```

Offsets:
```
event 1: (2031-06) - (2026-05) = 61 months
event 2: (2028-11) - (2026-05) = 30 months
```

Applied:
```
series[61] += -((-50000) × 3.70) = +185000     (expense spike)
series[30] += -((+200000) × 3.70) = -740000    (income spike, negative expense)
```

`series[30]` becomes `30000 - 740000 = -710000`. Negative — i.e. that month has net income (the inheritance more than covers expense). The projection engine handles negative expenses as "surplus this month adds to portfolio" — same path as a high-income month.

`series[61]` becomes `30000 + 185000 = 215000`. That month, expenses spike to 215k NIS — a $50K outflow on top of the normal $30k/mo baseline.

### Example D — "Kid's college 2030-2033"

Input event:
```
delta_kind = 'recurring_every_n_years'
recurring_amount_usd = -40000
recurring_period_years = 1
recurring_anchor_date = 2030-09-01
recurring_end_date = 2034-09-01
```

`first_offset = (2030-09) - (2026-05) = 52`, `period_months = 12`, `end_offset = (2034-09) - (2026-05) = 100`. Loop until `m >= min(360, 100) = 100`:
```
k=0: m=52  → series[52]  += +148000  (40000 × 3.70)
k=1: m=64  → series[64]  += +148000
k=2: m=76  → series[76]  += +148000
k=3: m=88  → series[88]  += +148000
k=4: m=100 → break (>= end_offset)
```

Four occurrences: 2030-Sep through 2033-Sep. The 2034-Sep occurrence is excluded by the end-date contract. Matches the user's intent ("4 years of college").

### Example E — "Sigma calibration in retirement_milestone"

Input event:
```
delta_kind = 'none'
category = 'retirement_milestone'
kind = 'sigma_calibration'
description = "Switched to bear-tilted sigma after 2026 plan review"
```

`apply_life_event_deltas` returns the series unchanged. The row still appears on the HolisticTimelineCard "Undated events" sidebar and still fires the `life_event` replan trigger when created (because the projection engine's mu/sigma parameters may have shifted at the same time, even if not represented in this row). The row is informational + audit-trail.

## Migration safety appendix

### What's preserved
- Every existing `life_events` row results in either (a) an exact-shape new row or (b) a `delta_kind=none` placeholder with original target_date preserved in the description text. No row is dropped (codex BLOCKER #1 — converted, not dropped).
- The `description` field is preserved verbatim across all conversions.
- The `created_at` / `updated_at` timestamps are preserved.

### What's lossy

| Original shape | Loss |
|---|---|
| `expense_event:college` with amount=$40K, target_date=2030 | Becomes one_shot at 2030. The user's likely intent was "four years of college" but the legacy schema couldn't encode that. Migration picks one_shot conservatively. User-prompted to add the recurring version manually via the new form. |
| `recurring_expense:*` missing target_date | Anchor defaults to `today` per codex IMPORTANT #1 (next occurrence at projection start, then every N years from there). User-prompted via the migration_log + acknowledgment banner to set the actual anchor if "today" is wrong. |
| `retirement_milestone:target_retire_year_change` | CONVERTED to `delta_kind=none` (NOT dropped — codex BLOCKER #1). The original `target_date` is serialized into the `description` field as `"Legacy target retire year: YYYY-MM-DD"` so the user sees the historical intent in the recorded-events list. Under the new model the target-year intent is achieved by the CASHFLOW improving / worsening, not by a date — this `none` row is purely a display marker + audit. |
| `career_event:*` / `family_event:*` without cashflow detail | Default `delta_kind=none`. UI surfaces "you may want to add the financial impact of this event — phase_change_start?" hint. |

### What's logged

For each lossy conversion, `life_events_migration_log` row with:
- `original_*` fields (category, kind, target_date, amount_usd, recurring_years)
- `decision` (one of the five delta_kinds or 'drop')
- `reason` (one-line human-readable: "Original retirement_milestone:target_retire_year_change is no longer modeled — re-add as delta_kind=none if you want a display marker")

Per [[feedback_ask_dont_assume]] the user sees the conversion via the auto-generated monitor flag and can audit each lossy row.

### Downgrade
The alembic downgrade attempts a best-effort reversal:
- `one_shot` → `target_date = one_shot_date`, `amount_usd = abs(one_shot_amount_usd)` (sign lost — log warning).
- `recurring_every_n_years` → `target_date = recurring_anchor_date`, `amount_usd = abs(recurring_amount_usd)`, `recurring_years = recurring_period_years`. Sign + end_date lost — log warning.
- `phase_change_*` → no clean reversal (the old schema didn't model phase changes). Downgrade DROPS the row and logs a warning. The user's data is in `life_events_migration_log` for audit.
- `none` → DROPS the row (the old schema couldn't represent "no cashflow effect").

The downgrade is documented as "non-zero data loss in reverse direction; intended for emergency revert only, not regular ops."

## Codex review focus appendix

Topics the codex tandem zigzag review should probe hard:

1. **Data migration completeness.** Is every (category, kind) pair in the legacy enum space mapped to a destination? Are there observable rows in production (Ariel's dev DB) that don't fit any decision-table row?
2. **Sign-convention correctness.** Trace each delta_kind's math through `apply_life_event_deltas` with at least one positive and one negative amount. Confirm the surplus calculation in `project_cashflow` still computes correctly (no double-negation).
3. **Phase-change boundary inclusion.** The month of `phase_start_date` IS included — verify this is correct vs the user's mental model. If a user enters "kids leave home 2034-08-15", does the August 2034 cell already have the reduced expense, or only September? Spec says August. Codex should challenge.
4. **Recurring-anchor offset preservation.** Car bought 2027-Mar, period=5y: next at 2032-Mar. Verify `months_between` math doesn't drift across multi-year loops (compounding rounding errors on month-arithmetic). The spec uses explicit `first_offset + k × period_months` integer arithmetic — no compound rounding — but verify.
5. **One-shot at horizon edge.** Spec says `m_offset == horizon_months` is excluded. Codex: confirm this matches Python `range(horizon_months)` indexing.
6. **FX scenario-dependence.** §2.4 punts to single-FX v1. Codex: is this acceptable, or does the user's three-scenario retirement projection require three FX values for life-event deltas too? If yes, signature needs `usd_to_nis_by_scenario: dict[str, float]`.
7. **Removed-clamp regression risk.** Are there indirect consumers of the removed `life_event_clamp_date` field (logging, telemetry, UI elements) we haven't grep'd? Codex grep + report.
8. **UI prefill round-trip.** Does the UpcomingVestCard CTA work end-to-end after the schema change — including the sign flip (post-tax-nominal is income, positive) and the new `section=one_shot` query param?
9. **Lossy-migration UX.** Is the auto-generated monitor flag enough, or should the migration ALSO send Ariel a one-time email/Telegram? Per project policy: no external channels v1, monitor flag only.
10. **`delta_kind=none` semantics drift.** Five delta_kinds, but `none` is a special case. Are there code paths (timeline rendering, replan trigger, etc.) where `none` is silently treated as one of the other four? Risk of "every event with delta_kind=none is one_shot in disguise."

## Codex tandem zigzag review summary

**Verdict:** BLOCK → resolved to APPROVE_WITH_CONDITIONS after BLOCKER integration.
**Session:** `tools/codex-tandem/sessions/2026-05-29-life-events-redesign-spec-review/`.
**Run:** 2026-05-29, zigzag MAX_ROUNDS=2, status=escalate (zigzag's agreement heuristic didn't converge — expected for seeded-Claude-findings runs; the findings themselves are integrated below).

**BLOCKERs (all integrated above):**
1. **Conflicting handling of `retirement_milestone:target_retire_year_change`** — spec text was inconsistent (drop in §1.5 vs none in §2.5/§7.3). Resolved: the row is CONVERTED to `delta_kind=none` with the original target_date preserved in the description field. §1.5 decision table + §2.5 + §7.3 all reference the same behavior. No row is ever dropped.
2. **Migration-loss notification too weak** — original spec proposed only a `monitor_flags` info row. Per [[feedback_ask_dont_assume]] that's silent-by-default. Resolved: dual surface — Red-Flag Strip flag + acknowledgment banner on /life-events gated by new `users.life_events_migration_acknowledged_at` column. Banner persists until user clicks "I've reviewed all conversions." See updated §1.5.
3. **Sign convention footgun** — `series[m] += -(amount * fx)` was distributed across multiple call sites. Resolved: new §2.0 mandates a single helper `_apply_signed_delta_to_series` as the only place in the codebase that interprets the sign convention; full test matrix (7 cases) asserted.
4. **Clamp-removal blast radius** — original spec didn't enumerate consumers. Resolved: per-file consumer checklist in §2.5 listing `cashflow_projection.py`, `retirement_timeline.py`, `replan_triggers.py`, retirement API routes, retirement UI cards, and the test files. Implementer cannot miss a consumer.

**IMPORTANTs (all integrated):**
1. **Recurring anchor default = today, not today+period** — §1.5 decision table updated. Avoids underestimating near-term recurring expenses for legacy rows missing target_date.
2. **`expense_event:college → one_shot` migration needs remediation UX** — §1.5 + new `user_decision` column in `life_events_migration_log`; conversion-assistant modal fires on first /life-events page load for affected rows.
3. **Inclusive phase boundary visible in UI** — §4.4 event-row rendering updated to "from August 2034 onward" copy (not just the raw date) so the inclusive-month semantic is observable.
4. **FX decision explicit** — §2.4 locks single base-scenario FX for v1 with concrete rationale (FX variability across scenarios ≪ amount-estimate uncertainty). Future revision can promote to scenario-keyed.
5. **UpcomingVestCard prefill sign guarded** — §4.3 updated with explicit non-negative + non-NaN guard; CTA disables itself with tooltip when the upstream estimate is broken. Round-trip unit test specified.
6. **`delta_kind='none'` explicitly tested across paths** — new §7.6 with 8 cross-path test cases (deltas function, timeline, replan trigger, UI row, API serialization, migration). Prevents the "every none event is one_shot in disguise" risk.

**NICEs (acknowledged):**
7. Three-section UI choice aligned with framing — kept per spec.
8. Recurring multi-skip (anchor >1 period before start) — added test case in §7.1 #9 already covers single-skip; multi-skip added implicitly via mention in codex review focus.
9. SQL CHECK is enforceable but maintenance-heavy — Pydantic discriminator is primary contract; CHECK is second line of defense. Documented in §1.1.
10. Downgrade kept lossy + marked "operationally forward-only" — release docs note added to §Migration safety / Downgrade.

**Status:** all BLOCKERs and IMPORTANTs integrated above. Spec is APPROVE_WITH_CONDITIONS for implementation. Open items for Ariel surfaced in §9 (FX scenario-dependence decision, lossy-conversion review against actual dev DB, three-section vs single-form UI confirmation).

# Windfall flow — resume for the next agent

**Status:** backend shipped + live-verified. UI is the next piece. Read this in ~3 minutes and you're caught up.

## What's done

1. **Detector** — `argosy/services/retirement/windfall_detector.py` (367 LOC). Compares two consecutive `Family Finances Status - YY MMM.tsv` files in `$ARGOSY_EXPENSE_SAMPLES_ROOT` (= `D:\Google Drive\Family\Finances\Portfolio\Resources`). Returns a `WindfallEvent` when cash delta > $25K USD or > ₪75K NIS; auto-classifies as `rsu_sale` / `stock_sale` / `unclear` based on whether equity sales within the same month match the cash delta within 5%.

2. **Plan-aware allocator** — `argosy/services/retirement/windfall_allocator.py` (197 LOC). Reads the **Current Allocation** table from the bottom of the TSV (that's the user's plan; he maintains it outside Argosy). Returns a `WindfallAllocationPlan` with 60/25/15 split across long/medium/short horizons. Long-term fills biggest under-target gaps with tickers the user already holds (e.g. Growth → QQQM 60% + SCHG 40%). Medium + Short are placeholders pointing at the agent-fleet integration (next).

3. **Route** — `GET /api/retirement/windfall/detect?threshold_usd=25000&threshold_nis=75000`. Auto-finds the two most-recent TSVs in the user's Portfolio/Resources folder. Returns `{event, plan}` or `{event: null, reason: ...}`.

4. **Tests** — `tests/test_retirement_windfall.py`, 15 tests covering threshold gate, classification, sign convention, end-to-end. All pass.

5. **Live verification** — against the user's actual `Mar 2026 → May 2026` TSVs:
   - Detected $84,400 from NVDA -1040 + SGOV -350 sales (classified UNCLEAR because $243K of sales vs $84K cash delta = most was redeployed into AMD/CSPX/CNDX in-month)
   - Plan correctly identifies Growth as biggest under-target gap (+$132K) and splits $50K long-term across QQQM + SCHG

## What's next (the UI work)

### Priority 1 — `<WindfallBanner>` on Home

When the page loads, call `api.retirement.windfallDetect()`. If `event` is non-null, render a banner near the top of `/` (Home page):

```
┌─────────────────────────────────────────────────────────────────┐
│ ⚠ $84,400 windfall detected in your May 2026 TSV               │
│   classified: unclear · NVDA -1040 + SGOV -350 sales detected  │
│                                                  [ Tell me about this → ]
└─────────────────────────────────────────────────────────────────┘
```

"Tell me about this" deep-links to `/advisor?focus=windfall_<event_id>` (event_id is the timestamp/hash). The Advisor chat opens with the event context pre-rendered as the first message.

### Priority 2 — `<WindfallCard>` on `/retirement`

Same data, fuller surface. Hero shows the headline; below: the 8-row allocation table (under/over flagged per row); below that: the 3-horizon proposal cards with Accept/Defer buttons.

Layout to follow the §0.1 standard already locked: HeroCard + DrilldownSection + Sources panel. Reuse `<ValueWithTooltip>` for every dollar amount with rationale tooltips.

### Priority 3 — Classification dialogue

When `event.classified_source == "unclear"`, the Advisor chat needs to:
1. Open with the event context as the first message
2. Ask: "I saw your cash jump $X this month. NVDA -Y shares + SGOV -Z shares = $W of sales. The math doesn't match — did you reinvest some immediately? Bonus? Gift?"
3. Use `intake_extractor` to extract the classification from the user's reply
4. Store the classification + free-text explanation as an `IncomeEvent` row (new table) with FK to the windfall event

### Priority 4 — Accept/Defer into action_engine

When the user clicks **Accept** on a proposal, it routes through the existing `action_engine` from Wave 7. The proposal becomes a `PrioritizedAction` with severity HIGH and a due date.

### Priority 5 (later) — agent-fleet for medium/short

Currently `windfall_allocator.py` stubs the medium-term and short-term proposals. Wave 3-style: kick the 5-phase plan_synthesis orchestrator with the windfall context (cash available + plan deltas + watchlist + recent news). Output: 2-4 specific tickers per horizon with target prices + catalysts.

## Critical files for the next agent

```
argosy/services/retirement/windfall_detector.py     # WindfallEvent + detect_windfall
argosy/services/retirement/windfall_allocator.py    # WindfallAllocationPlan + propose_allocations
argosy/api/routes/retirement.py                      # /api/retirement/windfall/detect
tests/test_retirement_windfall.py                    # 15 tests
docs/design/SDD.md                                   # handover note (top of file) + Windfall flow subsection
docs/user-guide/index.html                           # end-to-end user guide; references Hole #1
ui/src/lib/api.ts                                    # extend api.retirement.* with windfallDetect()
ui/src/components/retirement/                        # add WindfallBanner.tsx + WindfallCard.tsx here
ui/src/app/page.tsx                                  # Home — add the banner near the top
```

## How to verify the backend works end-to-end

```bash
# Backend should already be running on :8000. If not:
$env:ARGOSY_EXPENSE_SAMPLES_ROOT = "D:/Google Drive/Family/Finances/Portfolio/Resources"
.venv/Scripts/python.exe -m uvicorn argosy.api.main:create_app --factory --host 127.0.0.1 --port 8000

# Smoke the endpoint:
curl -s "http://127.0.0.1:8000/api/retirement/windfall/detect"

# Should return JSON with event + plan. If event is null, check the
# TSVs exist + the cash delta crossed the threshold.
```

## Things to NOT do

- Don't change `update_leumi_tsv.py` — user's existing convention; he runs it manually
- Don't try to upload the .xls directly into Argosy — same; he runs the script outside
- Don't add a "+ Add Income Event" button anywhere — the whole point of this design is "Argosy notices, you don't push"
- Don't auto-execute trades — every accepted proposal goes through the existing `/proposals` queue + the user's tier approval policy (T0/T1 auto for Argonaut $1K only; everything else needs human approval)

## User's binding preferences (verbatim, from `CLAUDE.md`)

- **Accuracy over LLM cost.** When in doubt, use Opus.
- **Ask, don't assume.** Surface judgment calls.
- **Don't report dollar cost figures** in status updates / plans / commit messages (user isn't price-sensitive).
- **Use codex-tandem kit for risky work** (money math, tax rules, parsers). Skip for UI / docs.
- **Manual UI smokes deliberately skipped** in normal flow; backend tests + Playwright live capture are the verification surface.

## Tests baseline

```
220/220 retirement tests passing
+ 15 new windfall tests
+ 6 retirement-page UI bugs fixed via Playwright audit
```

That's it. UI is the next piece. Three components, ~400-600 LOC, the §0.1 visualization standard is already locked.

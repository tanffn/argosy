# Shared research inputs

Implemented 2026-09-13. Input Sources manages YouTube subscriptions, public
RSS/Atom research, SEC 13D feeds, SEC 13F manager changes, and pasted letters.
Existing investor-event adapters are indexed without duplicating their pulls.

## Daily operation

The existing `youtube_subscriptions` registered job runs at 14:00 Asia/Jerusalem.
It queues every newly discovered upload before advancing the source cursor,
polls due document feeds, and processes up to three items per local day, at
most one per source. Priority, subject keywords and queue age determine order.
Paid analysis honors the existing cost guard; cheap collection still runs when
analysis is paused. Failed analyses retry after 24 hours, up to three attempts;
the UI supports an explicit retry. Expired processing leases can be recovered.
Cadences are minimum intervals checked by the daily job (not hourly schedules).

Documents use the existing four-reader research workflow: claims, skeptic,
portfolio implications and synthesis. Full accessible article text or PDFs are
read before analysis; restricted sources accept pasted originals. Source errors
remain visible. SEC access requires the deployment's declared contact email.
13F signals compare share counts against the preceding filing, exclude options,
and retain CUSIP/security identity and reporting-delay caveats. They do not
infer purchases merely from rising position dollar values.

## Decisions and discovery

The shared, bounded evidence packet reaches stock decisions, news analysts and
the thesis monitor, including material market outlook. URLs, speaker claims,
publication/observation dates, excerpts and skeptic findings remain attributed.
Sources cannot satisfy the existing evidence-sufficiency gate alone. Repeated
reports are not independent corroboration; prompts treat source text as data.

Synthesized WATCH/BUY ideas and material held-position implications create
idempotent preliminary review requests. Existing holdings keep the normal risk,
pushback, evidence and decision-team gates. New general research uses a separate
subject type from the 10x discovery mandate. Evidence request IDs participate in
the same-day decision fingerprint. WATCH enters the Argosy list; BUY creates an
Inbox action for review. This does not execute trades or force a verdict change.

## Ledger and measurement

Migration 0118 adds five tables: research_sources, research_items,
research_claims, research_review_requests and research_evidence_uses. Versions
and prior claims are retained. Original forecast horizons begin at publication,
not re-ingestion. Daily outcome evaluation compares explicit directional calls
with price moves and SPY; missing data stays pending, qualitative claims stay
unscored, and claims ingested after expiry are excluded from prospective scores.
This is price-direction calibration, not verification of every factual claim.

Input Sources shows collected/analyzed counts, tickers, claims, queue/errors,
cost, review histories, directional score sample size, and report-use receipts.
A receipt is written when an analyst report persists: supplied means in its
inputs; cited means the source ID or URL appears in its output. Changed verdicts
are measured on completed reviews, without asserting source causality.

## Operations and validation

`scripts/research_sources.py status|seed|backfill|poll` administers intake without
launching paid analysis. Backfill indexes saved YouTube artifacts without
rerunning agents. The UI's Sync and analyze button uses the registered job API,
so active-run locks and job history remain in effect.

Focused tests cover immutable versions, user isolation, forecast clocks, queue
budgets, durable upload cursors, deduplication, general research routing, usage
receipts, feed validation, 13F share changes and missing market data. Migration
upgrade/downgrade is checked separately; UI is checked with TypeScript.

---
name: verify-run
description: >
  Verify the run — post-hoc verification of a live Argosy run. Use when asked to
  "verify the run", "did the fleet work right", "check the last run", or after any
  live consult / deploy-cash / synthesis run. Reads db/argosy.db, logs/app/application.log
  and transcripts/<user>/<date>/ and judges: roster completeness, silent degradation,
  groundedness, verdict consistency, delivery.
---

# verify-run

Post-hoc judgment review of a REAL run from its actual artifacts. You re-derive blind
from raw evidence — never trust the agents' own prose about what happened.

**Args:** a `decision_run` id, or "latest", or a deploy/synthesis run reference.
User is `ariel` unless stated. Everything is READ-ONLY — never write to the DB or logs.

**Environment gotchas (verified):**
- There is NO `sqlite3` CLI on this machine. Use the venv Python:
  `.venv/Scripts/python.exe -c "import sqlite3; c = sqlite3.connect('file:db/argosy.db?mode=ro', uri=True); ..."`
- `agent_reports.decision_id` is a **VARCHAR** — query with `decision_id='128'` (string), not an int.
- Log lines in `logs/app/application.log` are structlog JSON: `{"event": "...", "level": "...", "timestamp": "2026-07-05T18:58:19.760754Z", ...}`. Rotation sibling: `application.log.1`.
- Transcript bundles: `transcripts/<user>/<YYYY-MM-DD>/<run_id>__<phase_kind>__<HHMMSS>__<hash>/` containing `TLDR.md`, `transcript.md`, `verdict.json`, `sequence.mmd`. The exact `bundle_dir` is stored on `decision_phases.bundle_dir`.
- **Raw agent input payloads live in `agent_reports.sources_json`** (list of `{source_id, content}`) — NOT in `user_prompt` (prompts reference payloads as attached document blocks). `user_prompt`/`system_prompt` columns hold the prompt text; `response_text` the raw output.

## Step 0 — scope the run

```
SELECT id, user_id, ticker, tier, started_at, finished_at, status,
       fund_manager_decision, decision_kind
FROM decision_runs WHERE id = :run;          -- or ORDER BY id DESC LIMIT 1 for "latest"

SELECT id, seq, kind, verdict_kind, bundle_dir, started_at, finished_at
FROM decision_phases WHERE decision_run_id = :run ORDER BY seq;

SELECT id, agent_role, model, tokens_in, tokens_out, confidence, created_at, phase_id
FROM agent_reports WHERE decision_id = CAST(:run AS TEXT) ORDER BY id;
```

Slice the log to the run's `[started_at, finished_at]` window (UTC). Beware: other
runs (sibling consults, the deploy team) share the window — correlate by
`decision_run_id`/`ticker` fields where present, and by role+timestamp+tokens_out
against the `agent_reports` rows where not (`agent.run.finished` has no run id).

## Check 1 — roster completeness

Expected roster comes from `argosy/decisions/flow.py` (read it, don't hardcode):
- analysts (per_ticker: fundamentals/news/sentiment/macro) → bull/bear debate
  (rounds: T1=1, T2/T3=2) + researcher_facilitator → trader → risk team
  (T1: neutral; T2/T3: aggressive+neutral+conservative) → fund_manager (T2/T3)
  → audit (T3). Deploy team roster: author + concentration/diversification/prudence
  reviewers in `argosy/services/deploy_decision_team.py` + `argosy/services/allocation_author/`.
- **Legitimate early exits (NOT failures):** trader `action=hold` closes the run with
  `status='hold'`, `fund_manager_decision='hold'` — risk team + fund manager never run,
  by design (`blocked_by='trader_hold'`, flow.py ~line 371). Same for
  `insufficient_data`. An analyst may be legitimately skipped WITH a recorded reason
  (see `per_ticker_analysts.done` → `skipped: [["sentiment", "empty_payload (no social data)"]]`).
- Cross-check log events (all names verified in code):
  `per_ticker_analysts.start` / `.done` (has `succeeded`, `skipped`, `unresolved_remediations`),
  `per_ticker_analysts.role_failed` / `.role_skipped_empty_citations` / `.quorum_failed`,
  `agent.run.finished` (has `agent_role`, `tokens_out`, `confidence`, `cost_usd`),
  `negotiation.phase.recorded`, `transcript_writer.bundle_written`,
  `decision_flow.record_phase_failed`.
- FAIL if: a roster member for the run's tier has no `agent_reports` row and no
  recorded skip reason; or a phase recorded but its bundle_dir is missing on disk.
- WARN if: any analyst-grade role has `tokens_out < ~300` (the known 0-token
  synthetic-verdict class) — read its `response_text` to judge.

## Check 2 — silent-degrade detection (fail-open seams)

Grep the run window for these EXACT event families (all verified present in `argosy/`):

- `deploy_team.reviewer_failed`, `deploy_team.fact_enrich_failed`,
  `deploy_team.flag_write_skipped` (swallowed IntegrityError in
  `deploy_decision_team.py` ~line 210), `deploy_cash.team_flag_sink_failed`,
  `deploy_cash.team_review_failed`, `deploy_cash.author_failed`,
  `deploy_cash.lookthrough_failed`, `deploy_cash.breakdown_failed`,
  `deploy_cash.preflight_failed`, `deploy_cash.fleet_review_failed`,
  `deploy_cash.market_context_failed`, `deploy_cash.candidate_research_failed`
- `fleet_reliability.circuit_open`, `fleet_reliability.breaker_open`,
  `fleet_reliability.transient_retry`, `fleet_reliability.kill_failed`
- `stock_decision.proposal_write_skipped` (swallowed IntegrityError in
  `argosy/services/stock_decision/service.py` ~line 155)
- `per_ticker_remediation.rerun_failed`, `per_ticker_remediation.cap_exhausted`
- `agent.parse_output.recovered_from_scan` (JSON parse fell back to scanning —
  tolerable at low rate; the SAME role recovering on every call = prompt/schema drift → WARN)
- `agent.hallucinated_sources` (cited source_ids not in supplied sources — flag-don't-strip,
  `argosy/agents/base.py` ~line 1252). **Known detector quirk:** the model citing a full
  URL that IS inside the payload body while the supplied source_id is e.g. `news/ELF`
  triggers this. Verify against `sources_json` content before calling it a real hallucination.
- generic: `"level": "warning"|"error"`, `*.failed`, `*_skipped`, `degraded`, `timeout`

For each hit: is it EXPLAINABLE (labeled downstream — e.g. deploy `dto.authored.degraded=true`,
a recorded skip reason, a successful retry) or SILENT (the run proceeded on partial inputs
with no label)? Silent → FAIL. Any `claude.exe` exit-1 / breaker with no
`fleet_reliability.transient_retry` following it → FAIL(reliability) — the known P0;
the wrapper covers the author only today.

## Check 3 — groundedness spot-check (blind re-derivation)

1. Open the run's final-phase bundle (`decision_phases.bundle_dir` of the highest seq):
   `verdict.json` + `transcript.md`.
2. Pick 2-3 load-bearing numbers in the verdict (multiples, growth rates, amounts, caps).
3. Re-derive each from the RAW payloads — `agent_reports.sources_json` of the upstream
   analyst reports for this run (the manifest/prose is a claim, not truth). Do NOT accept
   the agent's own arithmetic or a downstream agent quoting an upstream one.
4. FAIL any number that appears in the verdict but in NO raw payload. FAIL any BUY of a
   US-domiciled instrument other than NVDA (estate rule) regardless of what any gate said.

## Check 4 — verdict consistency

- The governing verdict is the **latest seq** in `decision_phases` — not the loudest row.
- `decision_runs.status` / `fund_manager_decision` must match that verdict
  (trader hold → status='hold').
- Severity honored: a reviewer/risk BLOCK objection ⇒ the thing was NOT shipped, or the
  override is explicitly recorded. Sums add up (sleeve amounts = deploy total ±rounding;
  action=hold ⇒ size 0, impact deltas "no change").
- Prose rationale agrees with the structured object (e.g. rationale says trim NVDA while
  the allocation adds NVDA-heavy exposure → FAIL).
- WARN: deciding agent `confidence='LOW'` with no escalation/second opinion recorded.

## Check 5 — delivery

- Anything the run flagged as actionable must exist in `action_proposals`
  (deploy team flags: `kind='deploy_team_flag'`; per-stock: `kind='stock_decision'`)
  and hence the inbox. A flag written nowhere = delivery bug → FAIL.
- **HOLD is first-class and SILENT by design** — a hold run correctly produces NO
  proposal and no flag; absence is a PASS there.
- `build_inbox` (`argosy/services/inbox/service.py`) drops failing sources into
  `dropped` — a proposal can exist in DB and never reach the inbox. If a
  needs-confirm-grade item exists, confirm it isn't in a `dropped` bucket.
- Check `monitor_flags` for the day and `job_runs` for scheduled jobs in ERROR with no
  corresponding flag.

## Output format

A short table: check → PASS / WARN / FAIL + one-line evidence (`file:line` or `table:id`).
Overall = worst check. For every FAIL, end with **"which agent should have caught this"**
and the team fix (inputs / blind-review / reliability). NEVER propose a per-symptom
deterministic gate — that is the whack-a-mole antipattern; the LLM team is the
architecture (CLAUDE.md binding rule). Report to the session; store nothing.

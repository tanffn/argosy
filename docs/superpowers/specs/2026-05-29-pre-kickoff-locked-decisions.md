# Pre-kickoff locked decisions — 2026-05-29 five-spec block

Companion file to the five specs landed on 2026-05-29:
- `2026-05-29-jobs-registry-design.md` (Spec A)
- `2026-05-29-state-observer-agent-design.md` (Spec B)
- `2026-05-29-predictions-ledger-design.md` (Spec C)
- `2026-05-29-life-events-cashflow-redesign-design.md` (Spec D)
- `2026-05-29-last-mile-delivery-design.md` (Spec E)

Each spec's "open items" section flagged decisions that needed user input before sprint kickoff. This file records the resolutions so implementing agents don't have to chase them.

## Resolutions

### Spec B — state observer agent

- **Backfill data coverage:** `expense_transactions` covers 2024-10-10 → 2026-05-10 (2,179 rows). 6-month window from current is well within range. **Empirical FX-emergence verification commit is unblocked.**
- **Portfolio snapshot history:** dev DB has only **1 portfolio snapshot** (`2026-03-24`). The observer's "diff vs last snapshot" capability is therefore a no-op until more monthly snapshots accumulate. **Backfill verification relies on the plan-baseline comparison path alone** (which is sufficient to surface the 3.6 → 2.8 USD/NIS deviation; plan assumes 3.6, current is 2.8, deviation visible without snapshot history). Spec B's commit-tests should not gate on snapshot-history detections.
- **`user_context.yaml` git history:** no committed history found for this filename pattern. Observer uses current values + lists `historical_replay_gaps` per spec's Appendix C.

### Spec D — /life-events cashflow redesign

- **Existing life_events rows:** dev DB has **0 rows** in `life_events` table. The data-migration concern from codex BLOCKER #1 (target_retire_year_change conversion) is therefore moot in practice. The migration code should still ship as specified (covers future state where rows might exist), but the conversion-log review step in Spec D §1.5 is **N/A** for this environment.
- **FX scenario shape:** locked to **single base-scenario FX** per codex IMPORTANT #4. Scenario-keyed FX deferred to a future spec.

### Spec E — last-mile delivery

- **Weekly email digest schedule:** **Friday 08:00 IDT.** Israeli work-week wind-down; aligned with the daily-news 17:00 IDT rhythm.
- **Inferred-life-event detector cadence:** **Daily 03:00 IDT.** Same nightly window as other heavy LLM jobs. Faster phase-change detection.
- **Inferred-life-event detector activation default:** **Shadow mode for first 30 days** — runs + logs candidates to a shadow table, surfaces nothing to user. Day-30 review confirms false-positive rate before flipping to live proposal surface. Lower nag risk during heuristic tuning.
- **VAPID `mailto:` subject:** `arieljacob@gmail.com` (Ariel's personal email; see [[user_email_personal_vs_work]] memory). NVIDIA work email is NOT used for personal app push contexts.
- **SMTP provider:** Argosy has no email infrastructure today (verified — `argosy/config.py` has no SMTP/email config). Spec E ships email via **`aiosmtplib`** (async, pure Python, no extra service dependency) reading config from env vars: `ARGOSY_SMTP_HOST`, `ARGOSY_SMTP_PORT`, `ARGOSY_SMTP_USERNAME`, `ARGOSY_SMTP_PASSWORD`, `ARGOSY_SMTP_FROM`. User provides any provider compatible with SMTP (Gmail, SES, Resend, etc.). Documented in Spec E §3.5.

### Cross-spec — bidirectional chat surface

User proposed (during open-items pass): "maybe we can open our own discord or whatsapp channel to talk? (not just push)". This is captured as **Spec F future scope** — a conversational surface (Discord first since the bot infra exists; WhatsApp follow-on via WhatsApp Business API). NOT in scope for the A/B/C/D/E sprint block. See `~/.claude/projects/D--Projects-financial-advisor/memory/project_bidirectional_chat_ambition.md` for the design ambition.

## Kickoff readiness

All nine open items resolved. The five specs are ready for sequential sprint execution: A → B → C → D → E. ~37-40 commits total. Per [[feedback_auto_mode_dont_check_in]] + [[feedback_parallelize_aggressively]] + [[feedback_use_tandem_for_risky_work]] — long uninterrupted session, codex tandem zigzag on risky commits, parallel sub-agents for independent units.

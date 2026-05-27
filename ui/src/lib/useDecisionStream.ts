"use client";
/**
 * useDecisionStream — live agent-cascade visibility hook.
 *
 * Merges two data sources into a unified decision-grouped view:
 *   1. Initial REST load from GET /api/agent-activity (persisted DB rows).
 *   2. Live WS events (agent.run.started, agent.run.finished) delivered via
 *      the extended useWSEvents hook.
 *
 * Cross-user filter: WS events whose payload.user_id !== userId are silently
 * dropped. This mirrors the pattern in ui/src/app/advisor/page.tsx and fixes
 * the SDD §15.4 known issue where home/proposals didn't filter on user_id.
 *
 * KNOWN LIMITATION: useWSEvents retains only the last event. If React
 * batches two setLast calls into a single render, the earlier event is lost.
 * We mitigate this by using the synchronous onEvent callback (bypasses React
 * batching) added to useWSEvents in Task 5. A comment below marks where this
 * risk still theoretically exists at the WS socket level.
 *
 * WS↔DB promotion: WS events carry run_correlation_id (BaseAgent.run).
 * Since migration 0028 the DB AgentReport also stores run_correlation_id,
 * enabling O(1) lookup when a REST row arrives with a non-null value.
 * For pre-migration legacy rows (run_correlation_id === null) the hook falls
 * back to the original ±10s + agent_role heuristic (findRestMatch).
 *
 * Unit tests: deferred — no test runner (jest/vitest) is wired in the UI
 * package. Tracked as a follow-up item.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type AgentActivityRow, type DecisionGroup as WireDecisionGroup } from "./api";
import { useWSEvents, type WSEvent } from "./ws";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * Unified agent row type merging REST (DB-persisted) and WS-live data.
 *
 * id === null  ⇒  WS-only entry; not yet matched to a persisted DB row.
 *                 The AgentDetailDrawer must check `id !== null` before
 *                 opening, since there is no DB record to fetch.
 *
 * We Omit<AgentActivityRow, "id"> to widen id from `number` to `number | null`.
 */
export type AgentRow = Omit<AgentActivityRow, "id"> & {
  /** null ⇒ WS-only (not yet flushed to DB / matched by REST poll). */
  id: number | null;
  status: "running" | "done" | "failed";
  run_correlation_id: string | null;
  started_at: string;
  finished_at: string | null;
  durationMs: number | null;
  /** turn_id is only populated from WS events; REST rows never carry it. */
  turn_id: string | null;
};

export type DecisionGroup = {
  /** decision_id | intake_session_id | "Standalone" */
  key: string;
  /** Ordered by started_at asc. */
  rows: AgentRow[];
  totalCostUsd: number;
  totalDurationMs: number | null;
  /** "running" if any row is running, "failed" if any row failed (and none
   *  running), otherwise "done". */
  status: "running" | "done" | "failed";
  /** Earliest started_at among all rows. */
  startedAt: string;
  /** Latest finished_at (null if any row is still running). */
  finishedAt: string | null;
  /** From /api/decisions/recent — null for WS-only (not yet persisted) entries. */
  tier: string | null;
  ticker: string | null;
  decision_kind: string | null;
  /** T4.4 — opaque blob from DecisionRun.notes_json. The row renderer
   *  parses it kind-specifically (delta_pushback -> delta_item_id;
   *  daily_brief -> brief_date). Null for synthesis runs and WS-only
   *  entries. */
  notes_json: string | null;
};

// ---------------------------------------------------------------------------
// Internal WS payload shapes
// ---------------------------------------------------------------------------

interface AgentRunStartedPayload {
  user_id?: string;
  agent_role?: string;
  run_correlation_id?: string;
  decision_id?: string | null;
  intake_session_id?: string | null;
  turn_id?: string | null;
  started_at?: string;
}

interface AgentRunFinishedPayload {
  user_id?: string;
  agent_role?: string;
  run_correlation_id?: string;
  decision_id?: string | null;
  intake_session_id?: string | null;
  turn_id?: string | null;
  finished_at?: string;
  status?: "done" | "failed";
}

type AgentRunPayload = AgentRunStartedPayload | AgentRunFinishedPayload;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Derive a group key from decision_id → intake_session_id → "Standalone".
 */
function groupKey(
  decision_id: string | null | undefined,
  intake_session_id: string | null | undefined,
): string {
  if (decision_id) return decision_id;
  if (intake_session_id) return intake_session_id;
  return "Standalone";
}

/**
 * Compute DecisionGroup.status from its constituent rows.
 */
function deriveGroupStatus(rows: AgentRow[]): "running" | "done" | "failed" {
  if (rows.some((r) => r.status === "running")) return "running";
  if (rows.some((r) => r.status === "failed")) return "failed";
  return "done";
}

/**
 * Compute DecisionGroup.finishedAt — null if any row is still running.
 */
function deriveGroupFinishedAt(rows: AgentRow[]): string | null {
  if (rows.some((r) => r.finished_at === null)) return null;
  // All rows have a finished_at — return the latest.
  const sorted = rows
    .map((r) => r.finished_at!)
    .sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  return sorted[sorted.length - 1] ?? null;
}

/**
 * Convert a persisted REST AgentActivityRow to an AgentRow (status: "done").
 * run_correlation_id is now stored on the DB row (migration 0028) and passed
 * through; NULL for pre-migration legacy rows.
 */
function restRowToAgentRow(r: AgentActivityRow): AgentRow {
  return {
    ...r,
    id: r.id,
    status: "done",
    run_correlation_id: r.run_correlation_id ?? null,
    started_at: r.created_at,
    finished_at: r.created_at,
    durationMs: null,
    turn_id: null,
  };
}

/**
 * Legacy fallback: try to match a WS-only AgentRow to a freshly-fetched REST
 * AgentActivityRow using the ±10s + agent_role heuristic.
 *
 * This is only called when the REST row has run_correlation_id === null (i.e.
 * it was persisted before migration 0028). Post-migration rows are matched via
 * O(1) byCorrelationId lookup in the finished-event handler instead.
 *
 * Heuristic:
 *   agent_role + user_id + decision_id (or both null) must match, AND
 *   the DB row's created_at must be within ±10 s of the WS finished_at
 *   (or started_at for running rows).
 *
 * excludeIds: DB ids already claimed by another WS row — used to prevent
 *   parallel agent runs (e.g. 3 concurrent "news" analysts in the same
 *   decision_id) from all promoting to the same persisted row (Blocker 2).
 *
 * Returns null if no match found.
 */
function findRestMatch(
  wsRow: AgentRow,
  restRows: AgentActivityRow[],
  excludeIds: Set<number>,
): AgentActivityRow | null {
  const refTime = wsRow.finished_at ?? wsRow.started_at;
  const refMs = new Date(refTime).getTime();
  const TOLERANCE_MS = 10_000;

  for (const r of restRows) {
    if (excludeIds.has(r.id)) continue; // already claimed by another WS row
    // Legacy rows only (pre-migration 0028): null run_correlation_id.
    if (r.run_correlation_id !== null) continue;
    if (r.agent_role !== wsRow.agent_role) continue;
    if (r.user_id !== wsRow.user_id) continue;
    if (r.decision_id !== wsRow.decision_id) continue;
    const rowMs = new Date(r.created_at).getTime();
    if (Math.abs(rowMs - refMs) <= TOLERANCE_MS) {
      return r;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useDecisionStream(
  userId: string,
  opts?: { turnId?: string; decisionId?: string },
): {
  decisions: DecisionGroup[];
  byCorrelationId: Map<string, AgentRow>;
  isLoading: boolean;
} {
  // Map from run_correlation_id → AgentRow.
  // WS-only entries have id === null; matched entries carry the DB id.
  const [byCorrelationId, setByCorrelationId] = useState<
    Map<string, AgentRow>
  >(new Map());

  // REST-sourced rows keyed by DB id (for grouping rows that arrived before
  // any WS events, or whose WS events were not captured).
  const [restRows, setRestRows] = useState<AgentActivityRow[]>([]);

  // Wire-side groups from /api/decisions/recent — used to populate tier/ticker/decision_kind.
  // Keyed by decision_id for O(1) lookup in the derive memo.
  const [wireGroupsMap, setWireGroupsMap] = useState<Map<string, WireDecisionGroup>>(new Map());

  const [isLoading, setIsLoading] = useState(true);

  /**
   * Processed-event dedup set. Bounded to MAX_PROCESSED_KEYS entries to prevent
   * unbounded memory growth during long-lived dashboard sessions (Blocker 4).
   * When the cap is exceeded the oldest half is evicted: eviction is O(n) but
   * triggered at most once per ~1 000 agent-run cycles — negligible in practice.
   * Key format: `${event.event}:${run_correlation_id}`.
   */
  const MAX_PROCESSED_KEYS = 2_000;
  // Insertion-ordered array of keys mirrors the Set so we can evict oldest half.
  const processedKeysRef = useRef<string[]>([]);
  const processedRef = useRef<Set<string>>(new Set());

  /**
   * Claimed DB ids — maps a DB AgentActivityRow.id to the run_correlation_id
   * that first matched it. Prevents parallel runs within the same decision_id
   * from all promoting to the same persisted row (Blocker 2).
   * Invariant: once an id is claimed it is never re-assigned.
   */
  const claimedDbIdsRef = useRef<Set<number>>(new Set());

  // ---------------------------------------------------------------------------
  // Initial REST load
  // isLoading starts as true (initial useState value); this effect sets it
  // false when the fetch settles. We intentionally do not set it back to true
  // here because the linter (react-hooks/set-state-in-effect) disallows
  // synchronous setState at the top of an effect body. The initial true value
  // is the canonical "loading" signal.
  // ---------------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setIsLoading(true);

      // Inline helper: fetch the flat /api/agent-activity feed. Used both
      // when /api/decisions/recent throws AND when it returns an empty
      // array (T5.2 — see below).
      const fetchAgentActivityFallback = async (): Promise<void> => {
        try {
          const resp = await api.agentActivity(userId, 100);
          if (cancelled) return;
          // Blocker 1 fix: functional merge so that any WS-triggered rows that
          // arrived during the async fetch are not wiped out.
          setRestRows((prev) => {
            const seen = new Set(prev.map((r) => r.id));
            const additions = resp.rows.filter((r) => !seen.has(r.id));
            return additions.length > 0 ? [...prev, ...additions] : prev;
          });
        } catch (fallbackErr: unknown) {
          // Non-fatal — surface in console but don't block the UI.
          console.warn("useDecisionStream: initial REST fetch failed", fallbackErr);
        }
      };

      try {
        // Preferred: /api/decisions/recent gives tier/ticker/decision_kind.
        const groups = await api.decisionsRecent(userId, 50);
        if (cancelled) return;

        // T5.2 — fall through to /api/agent-activity when /recent returns
        // an empty array (was only falling through on HTTP error). Advisor
        // / intake turns are intake_session_id-keyed (decision_id is NULL),
        // so /recent returns [] by design (it omits NULL decision_id rows
        // per Task 8) — meaning the DecisionAccordion would render empty
        // despite 10+ persisted turns living in agent_reports. The flat
        // /api/agent-activity endpoint surfaces those NULL-decision_id
        // rows, and the derive memo groups them under their
        // intake_session_id (see groupKey).
        if (groups.length === 0) {
          await fetchAgentActivityFallback();
          return;
        }

        // Build wire-groups map for the derive memo.
        const wgMap = new Map<string, WireDecisionGroup>();
        for (const g of groups) {
          wgMap.set(g.decision_id, g);
        }
        setWireGroupsMap(wgMap);
        // Flatten agent_runs into restRows (Blocker 1: functional merge).
        const flatRows = groups.flatMap((g) => g.agent_runs);
        setRestRows((prev) => {
          const seen = new Set(prev.map((r) => r.id));
          const additions = flatRows.filter((r) => !seen.has(r.id));
          return additions.length > 0 ? [...prev, ...additions] : prev;
        });
      } catch {
        // Fall back to flat agent-activity if the new endpoint is unavailable.
        await fetchAgentActivityFallback();
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [userId]);

  // ---------------------------------------------------------------------------
  // Process a single WS event — shared between onEvent (synchronous) and the
  // fallback useEffect path.
  // ---------------------------------------------------------------------------
  const processEvent = useCallback(
    (wsEvent: WSEvent<AgentRunPayload>) => {
      const { event, payload } = wsEvent;

      // Cross-user filter (SDD §15.4): drop events from other users.
      // Blocker 3 fix: require explicit match — missing user_id also dropped.
      if (payload.user_id !== userId) return;

      const correlationId = payload.run_correlation_id;
      if (!correlationId) return;

      // Idempotency guard — key on (event name, correlation id).
      // Blocker 4 fix: cap the dedup set to MAX_PROCESSED_KEYS so it doesn't
      // grow unboundedly during long-lived sessions. When the cap is hit, evict
      // the oldest half by clearing the set and re-populating from the second
      // half of the insertion-ordered keys array.
      const dedupKey = `${event}:${correlationId}`;
      if (processedRef.current.has(dedupKey)) return;
      processedRef.current.add(dedupKey);
      processedKeysRef.current.push(dedupKey);
      if (processedKeysRef.current.length > MAX_PROCESSED_KEYS) {
        const half = Math.floor(MAX_PROCESSED_KEYS / 2);
        const keep = processedKeysRef.current.slice(half);
        processedKeysRef.current = keep;
        processedRef.current = new Set(keep);
      }

      if (event === "agent.run.started") {
        const p = payload as AgentRunStartedPayload;
        const now = p.started_at ?? new Date().toISOString();

        setByCorrelationId((prev) => {
          const next = new Map(prev);
          // Don't overwrite a row that already arrived (e.g. if "finished"
          // came before "started" due to reordering).
          if (!next.has(correlationId)) {
            const wsRow: AgentRow = {
              // Stub the REST fields we don't have yet.
              id: null,
              user_id: userId,
              agent_role: p.agent_role ?? "unknown",
              decision_id: p.decision_id ?? null,
              intake_session_id: p.intake_session_id ?? null,
              model: "",
              confidence: null,
              tokens_in: 0,
              tokens_out: 0,
              cost_usd: 0,
              created_at: now,
              cache_input_tokens: 0,
              cache_creation_tokens: 0,
              thinking_tokens: 0,
              citations_count: 0,
              response_text: "",
              citations_json: null,
              prompt_hash: "",
              // Wave B-UI Task 9 — WS stubs don't carry sources.
              sources_preview: [],
              // AgentRow extras
              status: "running",
              run_correlation_id: correlationId,
              started_at: now,
              finished_at: null,
              durationMs: null,
              turn_id: p.turn_id ?? null,
            };
            next.set(correlationId, wsRow);
          }
          return next;
        });

        // Fetch updated REST rows so any just-persisted rows are captured.
        // We use a short lookback (5 s before started_at).
        const lookback = new Date(
          new Date(p.started_at ?? now).getTime() - 5_000,
        ).toISOString();
        api
          .agentActivity(userId, 20, { since: lookback })
          .then((resp) => {
            if (resp.rows.length > 0) {
              setRestRows((prev) => {
                const existingIds = new Set(prev.map((r) => r.id));
                const fresh = resp.rows.filter((r) => !existingIds.has(r.id));
                return fresh.length > 0 ? [...fresh, ...prev] : prev;
              });
            }
          })
          .catch((err: unknown) => {
            console.warn("useDecisionStream: REST refresh (started) failed", err);
          });
      } else if (event === "agent.run.finished") {
        const p = payload as AgentRunFinishedPayload;
        const finishedAt = p.finished_at ?? new Date().toISOString();

        setByCorrelationId((prev) => {
          const next = new Map(prev);
          const existing = next.get(correlationId);
          const startedAt = existing?.started_at ?? finishedAt;
          const startMs = new Date(startedAt).getTime();
          const finishMs = new Date(finishedAt).getTime();

          // Build a partial update on top of the existing WS row (if any).
          const updated: AgentRow = {
            ...(existing ?? {
              id: null,
              user_id: userId,
              agent_role: p.agent_role ?? "unknown",
              decision_id: p.decision_id ?? null,
              intake_session_id: p.intake_session_id ?? null,
              model: "",
              confidence: null,
              tokens_in: 0,
              tokens_out: 0,
              cost_usd: 0,
              created_at: finishedAt,
              cache_input_tokens: 0,
              cache_creation_tokens: 0,
              thinking_tokens: 0,
              citations_count: 0,
              response_text: "",
              citations_json: null,
              prompt_hash: "",
              // Wave B-UI Task 9 — WS stubs don't carry sources.
              sources_preview: [],
              run_correlation_id: correlationId,
              started_at: finishedAt,
              turn_id: p.turn_id ?? null,
            }),
            status: p.status === "failed" ? "failed" : "done",
            finished_at: finishedAt,
            durationMs: finishMs - startMs,
            // Preserve turn_id from started event if this one omits it.
            turn_id: p.turn_id ?? existing?.turn_id ?? null,
          };
          next.set(correlationId, updated);
          return next;
        });

        // Fetch the persisted REST row (the DB flush usually happens within
        // a few seconds of finished_at). Use a 5-second lookback window.
        const lookback = new Date(
          new Date(finishedAt).getTime() - 5_000,
        ).toISOString();
        api
          .agentActivity(userId, 20, { since: lookback })
          .then((resp) => {
            setRestRows((prev) => {
              const existingIds = new Set(prev.map((r) => r.id));
              const fresh = resp.rows.filter((r) => !existingIds.has(r.id));
              if (fresh.length === 0) return prev;

              // Try to promote WS-only byCorrelationId entries to DB-backed.
              //
              // O(1) path (post-migration 0028): if the REST row carries a
              // non-null run_correlation_id we can look it up directly in
              // byCorrelationId without the ±10s heuristic. This is exact and
              // handles multi-round same-agent runs correctly.
              //
              // Legacy fallback: rows with run_correlation_id === null (persisted
              // before migration 0028) still use the ±10s + agent_role heuristic
              // via findRestMatch. claimedDbIdsRef prevents parallel same-
              // decision_id agents from all claiming the same row (Blocker 2).
              setByCorrelationId((byCorrPrev) => {
                const byCorrNext = new Map(byCorrPrev);

                // O(1) path: index fresh rows by their run_correlation_id.
                const freshByCorrelation = new Map<string, AgentActivityRow>();
                for (const r of fresh) {
                  if (r.run_correlation_id !== null) {
                    freshByCorrelation.set(r.run_correlation_id, r);
                  }
                }

                for (const wsRow of byCorrNext.values()) {
                  if (wsRow.id !== null) continue; // already matched
                  const corrId = wsRow.run_correlation_id;

                  // O(1) lookup first (post-migration rows).
                  const directMatch =
                    corrId !== null ? freshByCorrelation.get(corrId) : undefined;
                  if (directMatch && !claimedDbIdsRef.current.has(directMatch.id)) {
                    claimedDbIdsRef.current.add(directMatch.id);
                    byCorrNext.set(corrId!, {
                      ...directMatch,
                      id: directMatch.id,
                      status: wsRow.status === "failed" ? "failed" : "done",
                      run_correlation_id: corrId,
                      started_at: wsRow.started_at,
                      finished_at: wsRow.finished_at ?? directMatch.created_at,
                      durationMs: wsRow.durationMs,
                      turn_id: wsRow.turn_id,
                    });
                    continue;
                  }

                  // Legacy fallback: ±10s heuristic for pre-migration rows.
                  const match = findRestMatch(
                    wsRow,
                    fresh,
                    claimedDbIdsRef.current,
                  );
                  if (match) {
                    claimedDbIdsRef.current.add(match.id);
                    byCorrNext.set(wsRow.run_correlation_id!, {
                      ...match,
                      id: match.id,
                      status: wsRow.status === "failed" ? "failed" : "done",
                      run_correlation_id: wsRow.run_correlation_id,
                      started_at: wsRow.started_at,
                      finished_at: wsRow.finished_at ?? match.created_at,
                      durationMs: wsRow.durationMs,
                      turn_id: wsRow.turn_id,
                    });
                  }
                }
                return byCorrNext;
              });

              return [...fresh, ...prev];
            });
          })
          .catch((err: unknown) => {
            console.warn("useDecisionStream: REST refresh (finished) failed", err);
          });
      }
    },
    [userId],
  );

  // ---------------------------------------------------------------------------
  // WS subscription — use synchronous onEvent callback to avoid React batching
  // dropping intermediate events (see module-level comment).
  // ---------------------------------------------------------------------------
  // KNOWN LIMITATION: if the WebSocket delivers two messages so quickly that
  // the browser fires both onmessage callbacks before React flushes any state,
  // the synchronous onEvent callback still fires for each message individually
  // and processEvent will be called twice. However, if the WS socket itself
  // coalesces messages at the OS buffer level, one onmessage may contain both.
  // This is an accepted risk for the current implementation.
  useWSEvents<AgentRunPayload>(
    ["agent.run.started", "agent.run.finished"],
    { onEvent: processEvent },
  );

  // ---------------------------------------------------------------------------
  // Derive decisions from byCorrelationId + any REST rows not in the WS map.
  // ---------------------------------------------------------------------------
  const turnId = opts?.turnId;
  const decisionId = opts?.decisionId;
  const decisions = useMemo<DecisionGroup[]>(() => {
    // Collect all rows: prefer WS-tracked entries (may be richer/more recent),
    // then add REST rows whose id doesn't appear in byCorrelationId yet.
    const allRows: AgentRow[] = Array.from(byCorrelationId.values());
    const trackedDbIds = new Set(
      allRows.filter((r) => r.id !== null).map((r) => r.id!),
    );
    for (const r of restRows) {
      if (!trackedDbIds.has(r.id)) {
        allRows.push(restRowToAgentRow(r));
      }
    }

    // Apply turnId filter if requested; else decisionId filter if requested.
    // turnId and decisionId are mutually exclusive — turnId wins if both set.
    const filtered = turnId
      ? allRows.filter((r) => r.turn_id === turnId)
      : decisionId
        ? allRows.filter((r) => r.decision_id === decisionId)
        : allRows;

    // Group by decision key.
    const groupMap = new Map<string, AgentRow[]>();
    for (const row of filtered) {
      const key = groupKey(row.decision_id, row.intake_session_id);
      const existing = groupMap.get(key);
      if (existing) {
        existing.push(row);
      } else {
        groupMap.set(key, [row]);
      }
    }

    // Build sorted DecisionGroup array.
    const groups: DecisionGroup[] = [];
    for (const [key, rows] of groupMap.entries()) {
      // Sort rows within a group by started_at asc.
      const sorted = [...rows].sort((a, b) =>
        a.started_at < b.started_at ? -1 : a.started_at > b.started_at ? 1 : 0,
      );
      const totalCostUsd = sorted.reduce((acc, r) => acc + (r.cost_usd ?? 0), 0);
      const allDurations = sorted.map((r) => r.durationMs);
      const totalDurationMs = allDurations.some((d) => d === null)
        ? null
        : allDurations.reduce<number>((acc, d) => acc + d!, 0);

      // Look up wire-side metadata (tier/ticker/decision_kind) by decision_id.
      // WS-only entries (key is an intake_session_id or "Standalone") won't have
      // a wire group — they get null for these fields, which is fine.
      const wireGroup = wireGroupsMap.get(key) ?? null;

      groups.push({
        key,
        rows: sorted,
        totalCostUsd,
        totalDurationMs,
        status: deriveGroupStatus(sorted),
        startedAt: sorted[0].started_at,
        finishedAt: deriveGroupFinishedAt(sorted),
        tier: wireGroup?.tier ?? null,
        ticker: wireGroup?.ticker ?? null,
        decision_kind: wireGroup?.decision_kind ?? null,
        // T4.4 — opaque blob; parsed in DecisionAccordion row renderer.
        notes_json: wireGroup?.notes_json ?? null,
      });
    }

    // Sort groups by startedAt desc (newest first).
    groups.sort((a, b) =>
      a.startedAt > b.startedAt ? -1 : a.startedAt < b.startedAt ? 1 : 0,
    );

    return groups;
  }, [byCorrelationId, restRows, turnId, decisionId, wireGroupsMap]);

  return { decisions, byCorrelationId, isLoading };
}

/**
 * Thin fetch helpers for the Argosy backend.
 *
 * Every call resolves to an absolute URL via `apiUrl(path)`. We DO NOT
 * route through the Next.js dev `rewrites()` proxy because the proxy
 * has an internal timeout (~30-60s) that surfaces as HTTP 500 in the
 * browser whenever an upstream agent call takes longer (intake/turn,
 * intake/upload, plan/critique can all easily exceed 60s on Haiku).
 *
 * Resolution:
 *   - `NEXT_PUBLIC_API_URL` env var if set (production: e.g. Vercel
 *     frontend + separate engine host).
 *   - Else `http://localhost:8000` (local dev — CORS in
 *     `argosy/api/main.py` allows http://localhost:1337 origin).
 */
function apiUrl(path: string): string {
  const base =
    typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_URL
      ? process.env.NEXT_PUBLIC_API_URL
      : "http://localhost:8000";
  return path.startsWith("http") ? path : `${base}${path}`;
}

// Re-export retirement primitives so call sites can `import { ValueWithRationale } from "@/lib/api"`.
export type {
  BLStipendResponse,
  GateStatus,
  GateVerdict,
  MekademBandResponse,
  RuinProbabilityResponse,
  SafetyGatesResponse,
  Source,
  SourcesResponse,
  ValueWithRationale,
  Verdict,
} from "@/lib/retirement-types";
import type {
  BLStipendResponse,
  MekademBandResponse,
  RuinProbabilityResponse,
  SafetyGatesResponse,
  Source,
  SourcesResponse,
  ValueWithRationale,
} from "@/lib/retirement-types";

export interface PortfolioPosition {
  location: string;
  currency: string;
  asset_type: string;
  details: string;
  symbol: string;
  shares: number | null;
  current_price: number | null;
  usd_value_k: number | null;
}

export interface PortfolioAllocation {
  category: string;
  pct: number | null;
  target_pct: number | null;
  delta_k: number | null;
}

export interface PortfolioSnapshotDTO {
  snapshot_date: string | null;
  fx_usd_nis: number | null;
  fx_usd_eur: number | null;
  total_usd_value_k: number;
  positions: PortfolioPosition[];
  allocations: PortfolioAllocation[];
  source_path: string | null;
  parse_warnings: string[];
}

export interface PlanCurrentDTO {
  plan_version_id: number | null;
  version_label: string | null;
  raw_markdown: string;
  imported_at: string | null;
  latest_critique_json: Record<string, unknown> | null;
  latest_critique_created_at: string | null;
}

export interface DailyBriefDTO {
  id: number;
  user_id: string;
  run_at: string;
  summary_text: string;
  news_report: Record<string, unknown> | null;
  macro_report: Record<string, unknown> | null;
  concentration_report: Record<string, unknown> | null;
  plan_delta: Record<string, unknown> | null;
  // T4.5 — runner-produced one-pager. ``content_md`` carries the
  // markdown body the home page renders front-and-center;
  // ``brief_date`` is the calendar date the brief covers (ISO
  // ``YYYY-MM-DD``); ``decision_run_id`` is the back-pointer to the
  // ``decision_runs`` row that produced it. All three are non-null on
  // T4.5 rows; legacy Phase 2 rows leave content_md="" and
  // brief_date=null.
  content_md: string;
  brief_date: string | null;
  decision_run_id: number | null;
}

export interface AgentActivityRow {
  id: number;
  user_id: string;
  agent_role: string;
  decision_id: string | null;
  model: string;
  confidence: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  created_at: string;
  cache_input_tokens: number;
  cache_creation_tokens: number;
  thinking_tokens: number;
  citations_count: number;
  // Wave B-UI drawer fields
  response_text: string;
  citations_json: string | null;
  prompt_hash: string;
  // Wave B-UI Task 5 — grouping key for intake-session agents.
  intake_session_id: string | null;
  // Wave B-UI Task 9 — lightweight source previews for the Sources tab.
  sources_preview: Array<{
    source_id: string;
    body_chars: number;
    body_head: string;
  }>;
  // Wave B-UI follow-up Item 2 — uuid4 from BaseAgent.run() (migration 0028).
  // NULL for rows persisted before the migration. Always present regardless of
  // detail= flag so useDecisionStream can do O(1) WS↔DB promotion.
  run_correlation_id: string | null;
}

export interface AgentActivityResponse {
  rows: AgentActivityRow[];
  next_since: string | null;
}

// Wave B-UI follow-up Item B — on-demand prompt payload for the Prompt tab.
// Fetched separately from the list because prompts are 10-100KB each.
export interface AgentPrompt {
  id: number;
  system_prompt: string;
  user_prompt: string;
}

export interface DecisionGroup {
  decision_id: string;
  decision_kind: string | null;
  tier: string | null;
  ticker: string | null;
  started_at: string;
  finished_at: string | null;
  status: string;
  total_cost_usd: number;
  agent_count: number;
  agent_runs: AgentActivityRow[];
  // T4.4 — raw notes_json blob from DecisionRun. Parsed kind-specifically
  // by the row renderer: delta_pushback surfaces `delta_item_id`,
  // daily_brief surfaces `brief_date`. Synthesis runs leave it null.
  notes_json?: string | null;
}

export interface ProposalListItem {
  id: number;
  user_id: string;
  ticker: string;
  action: string;
  size_shares_or_currency: number;
  size_units: string;
  instrument: string;
  order_type: string;
  tier: string;
  account_class: string;
  status: string;
  rationale_summary: string;
  confidence: string | null;
  cooling_off_until: string | null;
  created_at: string;
  updated_at: string;
  // T4.2: surface speculative-candidate metadata alongside every row so
  // the proposals page can split speculative (account_class="limited")
  // from regular plan deltas and show conviction + a top citation.
  // ``conviction`` mirrors ``confidence`` (the canonical column); kept
  // separate so the UI can adopt the user-facing "conviction" term
  // without renaming the underlying column.
  conviction: string | null;
  cited_sources: string[];
}

export interface ProposalListResponse {
  rows: ProposalListItem[];
  total: number;
}

export interface ReasoningTrailItem {
  id: number;
  agent_role: string;
  model: string;
  confidence: string | null;
  response_text: string;
  created_at: string;
}

export interface ProposalDetail {
  proposal: ProposalListItem;
  expected_impact: Record<string, unknown> | null;
  history: Array<Record<string, unknown>>;
  approvals: Array<Record<string, unknown>>;
  reasoning_trail: ReasoningTrailItem[];
  decision_run: Record<string, unknown> | null;
}

export interface ProposalActionResponse {
  status: string;
  proposal_id: number;
  message?: string;
}

export interface ExecuteResponse {
  status: string;
  proposal_id: number;
  broker: string;
  broker_order_id: string;
  paper: boolean;
  reason: string;
  fills: Array<Record<string, unknown>>;
}

export interface LotItem {
  id: number;
  user_id: string;
  account_id: string;
  ticker: string;
  lot_id_external: string;
  quantity: number;
  cost_basis_usd: number;
  acquired_at: string | null;
  source: string;
  imported_at: string;
}

export interface LotsResponse {
  rows: LotItem[];
  total: number;
}

export interface FillItem {
  id: number;
  user_id: string;
  proposal_id: number | null;
  broker: string;
  broker_order_id: string;
  ticker: string;
  action: string;
  quantity: number;
  price: number;
  commission: number;
  filled_at: string;
  paper: boolean;
}

export interface FillsResponse {
  rows: FillItem[];
  total: number;
}

export interface AuditItem {
  id: number;
  user_id: string;
  event_type: string;
  entity_type: string;
  entity_id: string;
  payload_json: string;
  created_at: string;
}

export interface AuditResponse {
  rows: AuditItem[];
  total: number;
}

// ----------------------------------------------------------------------
// Provenance Wave A: user-files catalog
// ----------------------------------------------------------------------

export interface UserFileItem {
  id: number;
  user_id: string;
  sha256: string;
  original_name: string;
  sanitized_name: string;
  mime_type: string;
  kind: string; // text | image | plan_markdown | broker_csv | other
  size_bytes: number;
  source: string; // chat_attachment | intake_upload | intake_file_to_text | cost_basis_import
  turn_uuid: string | null;
  intake_session_id: string | null;
  plan_version_id: number | null;
  decision_run_id: number | null;
  created_at: string;
  deleted_at: string | null;
}

export interface FilesListResponse {
  items: UserFileItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface ParticipantDTO {
  agent_role: string;
  agent_report_id: number;
  side: string | null;
  perspective: string | null;
  round: number | null;
  confidence: string | null;
  model: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
}

export interface PhaseDTO {
  id: number;
  seq: number;
  kind: string;
  started_at: string;
  finished_at: string | null;
  verdict_kind: string | null;
  verdict: Record<string, unknown> | null;
  tldr_md: string | null;
  sequence_mmd: string | null;
  participants: ParticipantDTO[];
  transcript_md_url: string;
}

export interface DecisionRunDTO {
  id: number;
  user_id: string;
  decision_kind: string | null;
  ticker: string | null;
  tier: string | null;
  started_at: string;
  finished_at: string | null;
  status: string | null;
  fund_manager_decision: string | null;
  proposal_id: number | null;
  notes_json: string | null;
}

export interface UserFileLite {
  id: number;
  original_name: string;
  kind: string;
  source: string;
  size_bytes: number;
  created_at: string;
}

export interface ReplayResponse {
  decision_run: DecisionRunDTO;
  phases: PhaseDTO[];
  inputs: { user_files: UserFileLite[] };
  sequence_mmd_full: string;
}

// ----------------------------------------------------------------------
// T0.5/T0.6: FM-rooted agent tree for /decisions/[id]
//
// Backend builder lives at argosy/services/agent_tree_builder.py and is
// exposed via GET /api/decisions/{id}/agent-tree?user_id=...
// Replaces the meaningless top-level sequence diagram with a structured
// DAG rooted at fund_manager, with per-agent status + adapter outcomes.
// ----------------------------------------------------------------------

export type AgentNodeStatus = "ok" | "degraded" | "failed" | "skipped";

export type AdapterNodeStatus = "ok" | "empty" | "http_error" | "exception";

export interface AdapterNode {
  adapter_name: string;
  target: string | null;
  status: AdapterNodeStatus;
  latency_ms: number;
  payload_size_bytes: number;
  http_status_code: number | null;
  error_text: string | null;
}

// One finding emitted by the codex_second_opinion (cross-engine gpt-5)
// reviewer. Populated by the backend only on the codex_second_opinion
// node; every other AgentNode keeps `codex_findings` as an empty array.
// Mirrors `argosy.services.agent_tree_builder.CodexFindingNode`.
export type CodexFindingSeverity = "BLOCKER" | "AMBER" | "YELLOW";

export interface CodexFinding {
  severity: CodexFindingSeverity;
  topic: string;
  detail: string;
  suggested_fix: string;
}

export interface AgentNode {
  agent_role: string;
  agent_report_id: number | null;
  status: AgentNodeStatus;
  confidence: string | null;
  model: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: number | null;
  side: string | null;
  perspective: string | null;
  response_excerpt: string;
  failure_reason: string | null;
  children: AgentNode[];
  adapters: AdapterNode[];
  // Populated only on the `codex_second_opinion` node — the structured
  // findings parsed from `CodexSecondOpinion.findings` by the backend.
  // Empty array on every other node so consumers can iterate without a
  // presence check.
  codex_findings: CodexFinding[];
  // Adaptive-thinking telemetry — actual `thinking_tokens` used by the
  // model on this agent call. `null` when the agent didn't run or when
  // the row predates adaptive-thinking telemetry. The UI hides the
  // field when 0 or null to avoid clutter on agents that don't think.
  thinking_tokens: number | null;
}

export interface AgentTreeStatusSummary {
  agents_ok: number;
  agents_failed: number;
  // "skipped" agents (didn't run at all, e.g. codex zigzag wasn't
  // triggered) are tracked separately from "failed" (ran but errored or
  // returned low confidence). The backend started splitting these out
  // after the codex_second_opinion node was added to the FM topology —
  // previously skipped+failed was conflated in agents_failed.
  agents_skipped: number;
  adapters_ok: number;
  adapters_failed: number;
}

// Per-run cost rollup surfaced under the agent tree. Mirrors the
// backend `CostBreakdown` dataclass in
// `argosy/services/agent_tree_builder.py`. ``by_phase`` keys are stable
// (`phase_1` .. `phase_5` + `phase_4_5_codex`); ``by_role`` keys are the
// raw `agent_reports.agent_role` strings. `top_3_agents` is already
// sorted desc by spend; `cost_per_phase_table` mirrors `by_phase` with
// agent counts for direct UI rendering.
export type CostPhaseKey =
  | "phase_1"
  | "phase_2"
  | "phase_3"
  | "phase_4"
  | "phase_4_5_codex"
  | "phase_5";

export interface CostPerPhaseRow {
  phase: CostPhaseKey;
  cost: number;
  agent_count: number;
}

export interface CostBreakdown {
  total_usd: number;
  by_phase: Record<CostPhaseKey, number>;
  by_role: Record<string, number>;
  top_3_agents: Array<[string, number]>;
  agent_count: number;
  cost_per_phase_table: CostPerPhaseRow[];
}

export interface AgentTreeResponse {
  decision_run_id: number;
  decision_kind: string;
  status_summary: AgentTreeStatusSummary;
  // T4.4 — `root` is null for non-synthesis kinds (delta_pushback,
  // daily_brief, trade_proposal, plan_amendment_chat). The UI surfaces a
  // kind-appropriate placeholder using `unsupported_reason` in that case.
  root: AgentNode | null;
  // T4.4 — populated when `root === null`. Human-readable explanation of
  // why no DAG was built; safe to render verbatim.
  unsupported_reason?: string | null;
  // Per-run cost rollup (total + by-phase + by-role + top-3). Always
  // present — empty rollup for runs with no agent_reports.
  cost_breakdown: CostBreakdown;
}

// ----------------------------------------------------------------------
// T4.1 — per-position thesis cards
// ----------------------------------------------------------------------

export type PositionVerdict = "HOLD" | "BUY" | "TRIM" | "SELL" | "ADD";
export type PositionConviction = "HIGH" | "MEDIUM" | "LOW";

export interface PositionThesisDTO {
  ticker: string;
  current_shares: number | null;
  current_weight_pct: number | null;
  current_usd_value: number | null;
  verdict: PositionVerdict;
  conviction: PositionConviction;
  reasoning_md: string;
  cited_sources: string[];
  target_weight_pct: number | null;
  target_shares: number | null;
}

// ----------------------------------------------------------------------
// Phase 5: Argonaut + security
// ----------------------------------------------------------------------

export interface ArgonautPosition {
  ticker: string;
  quantity: number;
  avg_cost: number | null;
  currency: string;
  asset_class: string;
}

export interface ArgonautStatus {
  user_id: string;
  account_id: string;
  size_usd: number;
  execution_mode: "paper" | "live" | "queue_only";
  autonomy_enabled: boolean;
  per_decision_max_pct: number;
  daily_loss_limit_pct: number;
  open_positions: ArgonautPosition[];
}

export interface ArgonautSnapshot {
  date: string;
  total_value_usd: number;
  cash_usd: number;
  positions_value_usd: number;
  day_pnl_usd: number;
}

export interface ArgonautSnapshotsResponse {
  rows: ArgonautSnapshot[];
}

export interface ArgonautTrade {
  id: number;
  ticker: string;
  action: string;
  quantity: number;
  price: number;
  commission: number;
  filled_at: string;
  paper: boolean;
  broker: string;
  broker_order_id: string;
}

export interface ArgonautTradesResponse {
  rows: ArgonautTrade[];
}

export interface ModeResponse {
  status: string;
  mode: string;
  message?: string;
}

export interface TOTPSetupResponse {
  secret: string;
  provisioning_uri: string;
}

export interface TOTPVerifyResponse {
  ok: boolean;
  last_verified_at: string | null;
  detail?: string;
}

export interface TOTPStatusResponse {
  enrolled: boolean;
  last_verified_at: string | null;
}

async function getJSON<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(apiUrl(path), { cache: "no-store", ...(init ?? {}) });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
  return (await res.json()) as T;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
  return (await res.json()) as T;
}

async function putJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(apiUrl(path), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    // Bubble up the server's detail string so the UI can surface a
    // meaningful error (e.g. "counter_position is required when ...").
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // ignore — fall back to status
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const api = {
  /**
   * Retirement-companion engine. Plan:
   * docs/superpowers/plans/2026-05-28-retirement-companion-overhaul.md
   * Wave 0 surfaces sources + reference; later waves add safety/projection/etc.
   */
  retirement: {
    sources: () => getJSON<SourcesResponse>("/api/retirement/sources"),
    source: (sourceId: string) =>
      getJSON<Source>(
        `/api/retirement/sources/${encodeURIComponent(sourceId)}`,
      ),
    reference: (key: string, userId: string) =>
      getJSON<ValueWithRationale>(
        `/api/retirement/reference/${encodeURIComponent(key)}?user_id=${encodeURIComponent(userId)}`,
      ),
    mekadem: (
      fundId: string,
      userId: string,
      balanceNis?: number,
    ) => {
      const params = new URLSearchParams({ user_id: userId });
      if (balanceNis !== undefined) {
        params.set("balance_nis", String(balanceNis));
      }
      return getJSON<MekademBandResponse>(
        `/api/retirement/mekadem/${encodeURIComponent(fundId)}?${params.toString()}`,
      );
    },
    safetyGates: (userId: string) =>
      getJSON<SafetyGatesResponse>(
        `/api/retirement/safety-gates?user_id=${encodeURIComponent(userId)}`,
      ),
    ruinProbability: (
      userId: string,
      opts?: {
        retirementAge?: number;
        years?: number;
        targetPSolvent?: number;
        nPaths?: number;
        seed?: number;
      },
    ) => {
      const params = new URLSearchParams({ user_id: userId });
      if (opts?.retirementAge !== undefined) params.set("retirement_age", String(opts.retirementAge));
      if (opts?.years !== undefined) params.set("years", String(opts.years));
      if (opts?.targetPSolvent !== undefined) params.set("target_p_solvent", String(opts.targetPSolvent));
      if (opts?.nPaths !== undefined) params.set("n_paths", String(opts.nPaths));
      if (opts?.seed !== undefined) params.set("seed", String(opts.seed));
      return getJSON<RuinProbabilityResponse>(
        `/api/retirement/projection/ruin-probability?${params.toString()}`,
      );
    },
    bituachLeumi: (
      userId: string,
      currentAge: number,
      contributionHistoryYears: number,
      spouseEligible: boolean = false,
    ) => {
      const params = new URLSearchParams({
        user_id: userId,
        current_age: String(currentAge),
        contribution_history_years: String(contributionHistoryYears),
        spouse_eligible: String(spouseEligible),
      });
      return getJSON<BLStipendResponse>(
        `/api/retirement/bituach-leumi?${params.toString()}`,
      );
    },
  },
  portfolioSnapshot: (userId: string) =>
    getJSON<PortfolioSnapshotDTO>(
      `/api/portfolio/snapshot?user_id=${encodeURIComponent(userId)}`,
    ),
  planCurrent: (userId: string) =>
    getJSON<PlanCurrentDTO>(
      `/api/plan/current?user_id=${encodeURIComponent(userId)}`,
    ),
  // Markdown export — fetch as text, browser-trigger save via Blob.
  // Returns the raw markdown body; the caller wires the download.
  planExportMarkdown: async (userId: string): Promise<string> => {
    const res = await fetch(
      apiUrl(
        `/api/plan/export?user_id=${encodeURIComponent(userId)}&format=markdown`,
      ),
      { cache: "no-store" },
    );
    if (!res.ok) {
      throw new Error(
        `HTTP ${res.status} for /api/plan/export?user_id=${userId}`,
      );
    }
    return res.text();
  },
  recritique: (userId: string) =>
    postJSON<{ status: string; critique_id: number | null; detail: string }>(
      "/api/plan/critique",
      { user_id: userId },
    ),
  dailyBriefLatest: (userId: string) =>
    getJSON<DailyBriefDTO | null>(
      `/api/daily-brief/latest?user_id=${encodeURIComponent(userId)}`,
    ),
  decisionsRecent: (
    userId: string,
    limit = 20,
    opts?: { decisionKind?: string },
  ): Promise<DecisionGroup[]> => {
    const qs = new URLSearchParams({
      user_id: userId,
      limit: String(limit),
    });
    // T4.4 — optional server-side filter. Accepted values: trade_proposal,
    // plan_revision, plan_amendment_chat, delta_pushback, daily_brief.
    if (opts?.decisionKind) qs.set("decision_kind", opts.decisionKind);
    return getJSON<DecisionGroup[]>(`/api/decisions/recent?${qs.toString()}`);
  },
  // Wave B-UI follow-up Item B — fetch full prompts on-demand for the Prompt tab.
  // Separate endpoint: prompts are 10-100KB and should not bloat list responses.
  agentActivityPrompt: (id: number, userId: string): Promise<AgentPrompt> =>
    getJSON<AgentPrompt>(
      `/api/agent-activity/${id}/prompt?user_id=${encodeURIComponent(userId)}`,
    ),
  agentActivity: (
    userId: string,
    limit = 10,
    opts?: { since?: string; detail?: boolean; decisionId?: string },
  ) => {
    const qs = new URLSearchParams({
      user_id: userId,
      limit: String(limit),
    });
    if (opts?.since) qs.set("since", opts.since);
    if (opts?.detail === false) qs.set("detail", "false");
    if (opts?.decisionId) qs.set("decision_id", opts.decisionId);
    return getJSON<AgentActivityResponse>(`/api/agent-activity?${qs.toString()}`);
  },
  proposalsList: (userId: string, status?: string) => {
    const qs = new URLSearchParams({ user_id: userId });
    if (status) qs.set("status", status);
    return getJSON<ProposalListResponse>(`/api/proposals?${qs.toString()}`);
  },
  proposalDetail: (userId: string, id: number) =>
    getJSON<ProposalDetail>(
      `/api/proposals/${id}?user_id=${encodeURIComponent(userId)}`,
    ),
  proposalApprove: (
    id: number,
    userId: string,
    secondFactor = false,
    channel: "dashboard" | "email" | "cli" = "dashboard",
  ) =>
    postJSON<ProposalActionResponse>(`/api/proposals/${id}/approve`, {
      user_id: userId,
      channel,
      second_factor: secondFactor,
    }),
  proposalReject: (id: number, userId: string, note = "") =>
    postJSON<ProposalActionResponse>(`/api/proposals/${id}/reject`, {
      user_id: userId,
      note,
    }),
  proposalEscalateTier: (id: number, userId: string, levels = 1) =>
    postJSON<ProposalActionResponse>(`/api/proposals/${id}/escalate-tier`, {
      user_id: userId,
      levels,
    }),
  proposalExecute: (id: number, userId: string) =>
    postJSON<ExecuteResponse>(`/api/proposals/${id}/execute`, {
      user_id: userId,
    }),
  fillsList: (userId: string, proposalId?: number) => {
    const qs = new URLSearchParams({ user_id: userId });
    if (proposalId !== undefined) qs.set("proposal_id", String(proposalId));
    return getJSON<FillsResponse>(`/api/fills?${qs.toString()}`);
  },
  lotsList: (userId: string, opts?: { accountId?: string; ticker?: string }) => {
    const qs = new URLSearchParams({ user_id: userId });
    if (opts?.accountId) qs.set("account_id", opts.accountId);
    if (opts?.ticker) qs.set("ticker", opts.ticker);
    return getJSON<LotsResponse>(`/api/lots?${qs.toString()}`);
  },
  auditList: (
    userId: string,
    opts?: {
      eventType?: string;
      entityType?: string;
      entityId?: string;
      since?: string;
      until?: string;
      limit?: number;
    },
  ) => {
    const qs = new URLSearchParams({ user_id: userId });
    if (opts?.eventType) qs.set("event_type", opts.eventType);
    if (opts?.entityType) qs.set("entity_type", opts.entityType);
    if (opts?.entityId) qs.set("entity_id", opts.entityId);
    if (opts?.since) qs.set("since", opts.since);
    if (opts?.until) qs.set("until", opts.until);
    if (opts?.limit !== undefined) qs.set("limit", String(opts.limit));
    return getJSON<AuditResponse>(`/api/audit?${qs.toString()}`);
  },

  // Provenance Wave A — user-files catalog list/stream.
  listFiles: (
    userId: string,
    opts?: {
      kind?: string;
      source?: string;
      since?: string;
      until?: string;
      includeDeleted?: boolean;
      limit?: number;
      offset?: number;
    },
  ) => {
    const qs = new URLSearchParams({ user_id: userId });
    if (opts?.kind) qs.set("kind", opts.kind);
    if (opts?.source) qs.set("source", opts.source);
    if (opts?.since) qs.set("since", opts.since);
    if (opts?.until) qs.set("until", opts.until);
    if (opts?.includeDeleted) qs.set("include_deleted", "true");
    if (opts?.limit !== undefined) qs.set("limit", String(opts.limit));
    if (opts?.offset !== undefined) qs.set("offset", String(opts.offset));
    return getJSON<FilesListResponse>(`/api/files?${qs.toString()}`);
  },
  fileContentUrl: (id: number, userId: string) =>
    apiUrl(
      `/api/files/${id}/content?user_id=${encodeURIComponent(userId)}`,
    ),
  getDecisionReplay: (decisionRunId: number, userId: string) =>
    getJSON<ReplayResponse>(
      `/api/decisions/${decisionRunId}/replay?user_id=${encodeURIComponent(userId)}`,
    ),
  // T0.6 — FM-rooted agent tree for /decisions/[id]. See AgentTreeResponse.
  getAgentTree: (decisionRunId: number, userId: string) =>
    getJSON<AgentTreeResponse>(
      `/api/decisions/${decisionRunId}/agent-tree?user_id=${encodeURIComponent(userId)}`,
    ),
  getPhaseTranscriptUrl: (
    decisionRunId: number,
    phaseId: number,
    userId: string,
  ) =>
    apiUrl(
      `/api/decisions/${decisionRunId}/phases/${phaseId}/transcript?user_id=${encodeURIComponent(userId)}`,
    ),

  // Phase 5: Argonaut limited account
  argonautStatus: (userId: string) =>
    getJSON<ArgonautStatus>(
      `/api/argonaut/status?user_id=${encodeURIComponent(userId)}`,
    ),
  argonautSnapshots: (userId: string, limit = 365) =>
    getJSON<ArgonautSnapshotsResponse>(
      `/api/argonaut/snapshots?user_id=${encodeURIComponent(userId)}&limit=${limit}`,
    ),
  argonautTrades: (userId: string, limit = 50) =>
    getJSON<ArgonautTradesResponse>(
      `/api/argonaut/trades?user_id=${encodeURIComponent(userId)}&limit=${limit}`,
    ),
  argonautMode: (userId: string, mode: "paper" | "live" | "queue_only") =>
    postJSON<ModeResponse>(`/api/argonaut/mode`, { user_id: userId, mode }),
  argonautForceSnapshot: (userId: string) =>
    postJSON<ArgonautSnapshot>(`/api/argonaut/snapshot`, {
      user_id: userId,
      mode: "paper",
    }),

  // Phase 5: TOTP
  totpStatus: (userId: string) =>
    getJSON<TOTPStatusResponse>(
      `/api/security/totp/status?user_id=${encodeURIComponent(userId)}`,
    ),
  totpSetup: (userId: string) =>
    postJSON<TOTPSetupResponse>(`/api/security/totp/setup`, { user_id: userId }),
  totpVerify: (userId: string, code: string) =>
    postJSON<TOTPVerifyResponse>(`/api/security/totp/verify`, {
      user_id: userId,
      code,
    }),

  // Phase 7: Domain KB
  domainKbTree: () => getJSON<DomainKbTreeNode>(`/api/domain-kb/tree`),
  domainKbFile: (path: string) =>
    getJSON<DomainKbFileResponse>(
      `/api/domain-kb/file?path=${encodeURIComponent(path)}`,
    ),
  domainKbReviewQueue: () =>
    getJSON<DomainKbReviewQueueResponse>(`/api/domain-kb/review-queue`),
  domainKbReviewApprove: (id: number) =>
    postJSON<{ status: string; id: number }>(
      `/api/domain-kb/review/${id}/approve`,
      {},
    ),
  domainKbReviewReject: (id: number) =>
    postJSON<{ status: string; id: number }>(
      `/api/domain-kb/review/${id}/reject`,
      {},
    ),

  // Phase 7: Intake
  intakeStatus: (userId: string) =>
    getJSON<IntakeStatusResponse>(
      `/api/intake/status?user_id=${encodeURIComponent(userId)}`,
    ),
  intakeTurn: (userId: string, lastUserMessage: string, currentStage?: string) =>
    postJSON<IntakeTurnResponse>(`/api/intake/turn`, {
      user_id: userId,
      last_user_message: lastUserMessage,
      current_stage: currentStage,
    }),

  // Phase 1 reframe: Advisor (persistent gap-tracker panel)
  // Wave 5: optional `attachments` triggers a multipart POST so the
  // backend can save them and route to vision / plan-distillation as
  // appropriate. No attachments → JSON body (cheaper, prompt-cache-friendly).
  advisorTurn: async (
    userId: string,
    lastUserMessage: string,
    opts?: {
      currentStage?: string;
      targetField?: string;
      historyExcerpt?: string;
      attachments?: File[];
      turnId?: string;
    },
  ): Promise<AdvisorTurnResponse> => {
    const attachments = opts?.attachments ?? [];
    if (attachments.length === 0) {
      return postJSON<AdvisorTurnResponse>(`/api/advisor/turn`, {
        user_id: userId,
        last_user_message: lastUserMessage,
        current_stage: opts?.currentStage,
        target_field: opts?.targetField,
        history_excerpt: opts?.historyExcerpt ?? "",
        ...(opts?.turnId ? { turn_id: opts.turnId } : {}),
      });
    }
    const fd = new FormData();
    fd.append("user_id", userId);
    fd.append("last_user_message", lastUserMessage);
    if (opts?.currentStage) fd.append("current_stage", opts.currentStage);
    if (opts?.targetField) fd.append("target_field", opts.targetField);
    fd.append("history_excerpt", opts?.historyExcerpt ?? "");
    if (opts?.turnId) fd.append("turn_id", opts.turnId);
    for (const f of attachments) {
      fd.append("attachments", f, f.name);
    }
    const res = await fetch(apiUrl(`/api/advisor/turn`), {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        if (j && typeof j.detail === "string") detail = j.detail;
      } catch {
        // ignore
      }
      throw new Error(detail);
    }
    return (await res.json()) as AdvisorTurnResponse;
  },
  advisorGaps: (userId: string) =>
    getJSON<AdvisorGapsResponse>(
      `/api/advisor/gaps?user_id=${encodeURIComponent(userId)}`,
    ),
  advisorHomeBrief: (userId: string) =>
    // 8s timeout — the home brief stitches cached state and shouldn't
    // ever take that long. Surfaces as an `AbortError` (DOMException)
    // so the card can render "Couldn't reach advisor service" copy.
    getJSON<AdvisorHomeBriefResponse>(
      `/api/advisor/home-brief?user_id=${encodeURIComponent(userId)}`,
      { signal: AbortSignal.timeout(8000) },
    ),
  intakeFileToText: async (file: File): Promise<FileToTextResponse> => {
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(apiUrl("/api/intake/file-to-text"), {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (body?.detail) detail = String(body.detail);
      } catch {
        /* non-JSON body */
      }
      throw new Error(detail);
    }
    return (await res.json()) as FileToTextResponse;
  },
  intakeUpload: async (userId: string, file: File): Promise<IntakeUploadResponse> => {
    const fd = new FormData();
    fd.append("user_id", userId);
    fd.append("file", file);
    // Plan extraction takes ~60-90s on Haiku for a typical plan markdown.
    // The Next.js dev server's `rewrites()` proxy will drop the connection
    // before the upstream finishes, surfacing as HTTP 500 in the browser
    // even though the FastAPI request completes successfully behind the
    // proxy. To avoid the proxy timeout, hit the API directly.
    //
    // In dev: NEXT_PUBLIC_API_URL is unset and we hardcode localhost:8000.
    // In production (Vercel + Fly.io etc.): set NEXT_PUBLIC_API_URL to the
    // engine's public URL and the same direct call works.
    //
    // CORS in argosy/api/main.py already allows http://localhost:1337.
    const apiBase =
      typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_URL
        ? process.env.NEXT_PUBLIC_API_URL
        : "http://localhost:8000";
    const res = await fetch(`${apiBase}/api/intake/upload`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        if (body?.detail) detail = String(body.detail);
      } catch {
        /* non-JSON body */
      }
      throw new Error(detail);
    }
    return (await res.json()) as IntakeUploadResponse;
  },

  // Phase 7: Settings
  getAgentSettings: (userId: string) =>
    getJSON<Record<string, unknown>>(
      `/api/settings?user_id=${encodeURIComponent(userId)}`,
    ),
  patchAgentSettings: (userId: string, patch: Record<string, unknown>) =>
    fetch(`/api/settings`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, patch }),
    }).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status} for /api/settings`);
      return (await r.json()) as Record<string, unknown>;
    }),

  // ----------------------------------------------------------------------
  // Wave 1: Baseline distillate
  // ----------------------------------------------------------------------

  planBaseline: (userId: string) =>
    getJSON<BaselineResponse>(
      `/api/plan/baseline?user_id=${encodeURIComponent(userId)}`,
    ),
  planBaselineDistill: (userId: string, preserveUserEdits = true) =>
    postJSON<BaselineResponse>(
      `/api/plan/baseline/distill?user_id=${encodeURIComponent(userId)}&preserve_user_edits=${preserveUserEdits}`,
      {},
    ),
  planBaselineDistillateEdit: (
    userId: string,
    category: string,
    itemLabel: string,
    body: {
      value?: string | number;
      rationale?: string;
      detail?: string;
      rule?: string;
      user_edit_note?: string;
    },
  ) =>
    fetch(
      apiUrl(
        `/api/plan/baseline/distillate/${encodeURIComponent(category)}/${encodeURIComponent(itemLabel)}?user_id=${encodeURIComponent(userId)}`,
      ),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ).then(async (r) => {
      if (!r.ok)
        throw new Error(`HTTP ${r.status} for /api/plan/baseline/distillate`);
      return (await r.json()) as BaselineResponse;
    }),

  // ----------------------------------------------------------------------
  // Wave 2: synthesis flow + draft lifecycle
  // ----------------------------------------------------------------------

  planDraft: (userId: string) =>
    getJSON<DraftResponse>(`/api/plan/draft?user_id=${encodeURIComponent(userId)}`),
  // Home-page action-items widget. Reads dated short/medium-horizon
  // actions out of the user's pending draft (or current accepted plan
  // when no draft exists) and surfaces them as a checklist with
  // OVERDUE / TODAY / DUE_SOON / UPCOMING status tones. window_days
  // defaults to 14 server-side.
  planActionItems: (userId: string, windowDays = 14) =>
    getJSON<ActionItemsResponse>(
      `/api/plan/action-items?user_id=${encodeURIComponent(userId)}&window_days=${windowDays}`,
    ),
  // Live snapshot of the user's currently-running synthesis run, used by
  // the /plan page to render a "Synthesis #N · phase X of 5" card when
  // /api/plan/draft has 404'd because the prior draft was superseded.
  // Returns 200 + ``in_flight_synthesis=null`` when there's no run in
  // flight — never 404 — so the UI's polling loop can treat the result
  // as a single nullable state without an exception branch every tick.
  planInFlightSynthesis: (userId: string) =>
    getJSON<InFlightSynthesisResponse>(
      `/api/plan/in-flight-synthesis?user_id=${encodeURIComponent(userId)}`,
    ),
  planDraftObjections: (userId: string) =>
    getJSON<FMObjectionsResponse>(
      `/api/plan/draft/objections?user_id=${encodeURIComponent(userId)}`,
    ),
  // T4.8b — history of one item_id across plan_versions for the user.
  planItemHistory: (userId: string, itemId: string) =>
    getJSON<{
      item_id: string;
      entries: Array<{
        plan_version_id: number;
        version_label: string | null;
        role: string;
        drafted_at: string;
        horizon: string;
        summary: string;
        label: string;
        value: number | string | null;
        unit: string | null;
        rationale: string;
        accepted: boolean;
      }>;
    }>(
      `/api/plan/item-history?user_id=${encodeURIComponent(userId)}&item_id=${encodeURIComponent(itemId)}`,
    ),
  planDraftObjectionTranslate: (
    userId: string,
    body: {
      topic: string;
      detail: string;
      severity: string;
      cited_sources?: string[];
    },
  ) =>
    postJSON<{
      headline: string;
      plain_english: string;
      recommended_actions: string[];
      cited_sources: string[];
    }>(
      `/api/plan/draft/objections/translate?user_id=${encodeURIComponent(userId)}`,
      body,
    ),
  // Per-FM-objection user stance + start-new-round flow. See
  // argosy/api/routes/plan_objection_state.py for the backend.
  planDraftObjectionStateGet: (userId: string, planVersionId: number) =>
    getJSON<FMObjectionStateMapResponse>(
      `/api/plan/draft/objections/state?user_id=${encodeURIComponent(
        userId,
      )}&plan_version_id=${planVersionId}`,
    ),
  planDraftObjectionStatePut: (body: {
    user_id: string;
    plan_version_id: number;
    objection_index: number;
    stance: "AGREE" | "DISAGREE" | "DEFER";
    counter_position?: string | null;
    topic?: string;
    detail?: string;
  }) =>
    putJSON<{
      status: string;
      objection_index: number;
      stance: string;
    }>(`/api/plan/draft/objections/state`, body),
  planDraftObjectionsStartNewRound: (userId: string, planVersionId: number) =>
    postJSON<{
      status: string;
      decision_run_id: number;
      decision_audit_token: string;
      n_agreed: number;
      n_disagreed: number;
      n_deferred: number;
      guidance_preview: string;
    }>(
      `/api/plan/draft/objections/start-new-round?user_id=${encodeURIComponent(
        userId,
      )}&plan_version_id=${planVersionId}`,
      {},
    ),
  // FM-objection ZigZag (T4.9) — slim FM↔analyst dialogue per objection.
  // POST kicks off the 3-turn dialogue on a background thread; the UI
  // subscribes to ``plan.fm_objection.dialogue.completed`` WS events for
  // completion. GET re-renders prior dialogues after a page reload so
  // the dialogue state isn't lost.
  planDraftObjectionDiscuss: (
    objectionIndex: number,
    body: { user_id: string; analyst_role: string },
  ) =>
    postJSON<FMObjectionDiscussResponse>(
      `/api/plan/draft/objections/${objectionIndex}/discuss`,
      body,
    ),
  planDraftObjectionDialogues: (objectionIndex: number, userId: string) =>
    getJSON<FMObjectionDialoguesResponse>(
      `/api/plan/draft/objections/${objectionIndex}/dialogues?user_id=${encodeURIComponent(
        userId,
      )}`,
    ),
  planDraftNvdaTrajectory: (userId: string) =>
    getJSON<NvdaTrajectoryResponse>(
      `/api/plan/draft/nvda-trajectory?user_id=${encodeURIComponent(userId)}`,
    ),
  planDraftCashflowProjection: (
    userId: string,
    years = 30,
    retirementAge = 49,
    taxRate = 0.25,
    muNominalAnnual = 0.08,
    portfolioValueUsdOverride: number | null = null,
    sigmaAnnual = 0.18,
    lifestyleDriftAnnual = 0.0,
  ) => {
    const params = new URLSearchParams({
      user_id: userId,
      years: String(years),
      retirement_age: String(retirementAge),
      tax_rate: String(taxRate),
      mu_nominal_annual: String(muNominalAnnual),
      sigma_annual: String(sigmaAnnual),
      lifestyle_drift_annual: String(lifestyleDriftAnnual),
    });
    if (portfolioValueUsdOverride != null) {
      params.set("portfolio_value_usd_override", String(portfolioValueUsdOverride));
    }
    return getJSON<CashflowProjectionResponse>(
      `/api/plan/draft/cashflow-projection?${params.toString()}`,
    );
  },
  planDraftCashflowMonteCarlo: (
    userId: string,
    {
      years = 40,
      retirementAge = 49,
      taxRate = 0.25,
      muNominalAnnual = 0.08,
      sigmaAnnual = 0.18,
      lifestyleDriftAnnual = 0.0,
      portfolioValueUsdOverride = null,
      nPaths = 1000,
      seed = null,
    }: {
      years?: number;
      retirementAge?: number;
      taxRate?: number;
      muNominalAnnual?: number;
      sigmaAnnual?: number;
      lifestyleDriftAnnual?: number;
      portfolioValueUsdOverride?: number | null;
      nPaths?: number;
      seed?: number | null;
    } = {},
  ) => {
    const params = new URLSearchParams({
      user_id: userId,
      years: String(years),
      retirement_age: String(retirementAge),
      tax_rate: String(taxRate),
      mu_nominal_annual: String(muNominalAnnual),
      sigma_annual: String(sigmaAnnual),
      lifestyle_drift_annual: String(lifestyleDriftAnnual),
      n_paths: String(nPaths),
    });
    if (portfolioValueUsdOverride != null) {
      params.set("portfolio_value_usd_override", String(portfolioValueUsdOverride));
    }
    if (seed != null) {
      params.set("seed", String(seed));
    }
    return getJSON<MonteCarloProjectionResponse>(
      `/api/plan/draft/cashflow-monte-carlo?${params.toString()}`,
    );
  },
  planDraftTargetProgress: (userId: string) =>
    getJSON<TargetProgressResponse>(
      `/api/plan/draft/target-progress?user_id=${encodeURIComponent(userId)}`,
    ),
  decisionsRun: (body: DecisionRunRequest) =>
    postJSON<DecisionRunResponse>(`/api/decisions/run`, body),
  planDraftAccept: (draftId: number, userId: string) =>
    postJSON<{ status: string; new_current_id: number }>(
      `/api/plan/draft/${draftId}/accept?user_id=${encodeURIComponent(userId)}`,
      {},
    ),
  planDraftReject: (draftId: number, userId: string, reason: string, guidance = "") =>
    postJSON<{ status: string; draft_id: number }>(
      `/api/plan/draft/${draftId}/reject?user_id=${encodeURIComponent(userId)}`,
      { reason, guidance },
    ),
  planDraftDeltaAccept: (draftId: number, itemId: string, userId: string) =>
    postJSON<{ status: string }>(
      `/api/plan/draft/${draftId}/items/${encodeURIComponent(itemId)}/accept?user_id=${encodeURIComponent(userId)}`,
      {},
    ),
  planDraftDeltaReject: (
    draftId: number,
    itemId: string,
    userId: string,
    reason: string = "",
  ) =>
    postJSON<{ status: string; draft_id: number; item_id: string }>(
      `/api/plan/draft/${draftId}/items/${encodeURIComponent(itemId)}/reject?user_id=${encodeURIComponent(userId)}`,
      { reason },
    ),
  planDraftDeltaPushback: (
    draftId: number,
    itemId: string,
    userId: string,
    feedback: string,
  ) =>
    // T4.3 — the response now carries a ``decision_run_id`` for the
    // slim re-debate the backend kicked off. The UI subscribes to
    // ``plan.delta.pushback.completed`` WS events keyed on this id and
    // can navigate to /decisions/<id> for the verdict trail.
    //
    // ``status`` values:
    //   - "slim_redebate_started"        — flow kicked off; run_id is real
    //   - "slim_redebate_inflight"       — idempotent; run_id is the existing one
    //   - "cost_cap_refused"             — refused cleanly (run_id is null)
    //   - "slim_redebate_failed_to_start" — dispatcher errored (rare; logged)
    //   - "pushback_recorded"            — slim flow disabled via env var
    postJSON<{
      status: string;
      draft_id: number;
      item_id: string;
      feedback: string;
      decision_run_id: number | null;
      inflight: boolean;
      detail: string | null;
    }>(
      `/api/plan/draft/${draftId}/items/${encodeURIComponent(itemId)}/pushback?user_id=${encodeURIComponent(userId)}`,
      { feedback },
    ),
  planDraftDeltaEdit: (
    draftId: number,
    itemId: string,
    userId: string,
    body: { proposed?: Record<string, unknown>; user_edit_note?: string },
  ) =>
    fetch(
      apiUrl(
        `/api/plan/draft/${draftId}/items/${encodeURIComponent(itemId)}?user_id=${encodeURIComponent(userId)}`,
      ),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ).then(async (r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return (await r.json()) as { status: string };
    }),
  advisorCheckIn: (userId: string, guidance = "") =>
    postJSON<{
      status: string;
      decision_run_id: number;
      decision_audit_token: string; // e.g. "plan-synth-42" — UI uses verbatim as cascade filter key
      draft_id: number | null; // null until plan.draft.completed WS event fires
    }>(
      `/api/advisor/check-in`,
      { user_id: userId, guidance, urgency: "now" },
    ),
  // T2.3 — Resume a failed synthesis from the first incomplete phase.
  // Returns { resume_from_phase, skipped_phases, decision_audit_token }.
  advisorCheckInResume: (
    userId: string,
    decisionRunId: number,
    guidance = "",
  ) =>
    postJSON<{
      status: string;
      decision_run_id: number;
      decision_audit_token: string;
      resume_from_phase: number;
      skipped_phases: number[];
    }>(
      `/api/advisor/check-in/${decisionRunId}/resume`,
      { user_id: userId, guidance, urgency: "now" },
    ),

  // ----------------------------------------------------------------------
  // Wave 3: speculative candidates ("Take a swing")
  // ----------------------------------------------------------------------

  // GET the user's accepted plan in the structured DraftResponse shape so
  // the Argonaut page can read horizon_short.speculative_candidates.  The
  // legacy `/api/plan/current` returns a different DTO (raw markdown +
  // latest critique) consumed by the home + /plan pages, so this endpoint
  // lives at `/current/structured` to avoid colliding.
  planCurrentStructured: (userId: string) =>
    getJSON<DraftResponse>(
      `/api/plan/current/structured?user_id=${encodeURIComponent(userId)}`,
    ),
  planSpeculativeTake: (
    userId: string,
    ticker: string,
    executionMode: "paper" | "live" = "paper",
  ) =>
    postJSON<{ status: string; proposal_id: number; ticker: string; paper: boolean }>(
      `/api/plan/current/speculative/${encodeURIComponent(ticker)}/take?user_id=${encodeURIComponent(userId)}&execution_mode=${executionMode}`,
      {},
    ),

  // ----------------------------------------------------------------------
  // T4.1: per-position thesis cards
  // ----------------------------------------------------------------------

  positionTheses: (userId: string) =>
    getJSON<PositionThesisDTO[]>(
      `/api/positions/thesis?user_id=${encodeURIComponent(userId)}`,
    ),

  // ----------------------------------------------------------------------
  // Wealth Dashboard — top-of-/portfolio retirement projection + 6 stat
  // cards (cash runway, NVDA concentration, savings rate, FX exposure,
  // RSU income, US-situs estate exposure). Pure-Python aggregator;
  // see argosy/services/wealth_dashboard.py for per-block semantics.
  // ----------------------------------------------------------------------

  wealthDashboard: (userId: string) =>
    getJSON<WealthDashboardDTO>(
      `/api/portfolio/wealth-dashboard?user_id=${encodeURIComponent(userId)}`,
    ),

  // ----------------------------------------------------------------------
  // Wave 4: plan amendment chat flow
  // ----------------------------------------------------------------------

  advisorAmendmentCancel: (userId: string, decisionRunId: number) =>
    postJSON<{ status: string; decision_run_id: number }>(
      `/api/advisor/amendment/${decisionRunId}/cancel?user_id=${encodeURIComponent(userId)}`,
      {},
    ),

  // ----------------------------------------------------------------------
  // Fleet self-review (migration 0037) — anomaly detector reports.
  // ----------------------------------------------------------------------

  fleetSelfReviewLatest: (userId: string) =>
    getJSON<FleetSelfReviewDTO | null>(
      `/api/fleet-self-review/latest?user_id=${encodeURIComponent(userId)}`,
    ),

  fleetSelfReview: (userId: string, id: number) =>
    getJSON<FleetSelfReviewDTO>(
      `/api/fleet-self-review/${id}?user_id=${encodeURIComponent(userId)}`,
    ),

  fleetSelfReviewList: (userId: string, limit: number = 50) =>
    getJSON<FleetSelfReviewListItemDTO[]>(
      `/api/fleet-self-review/list?user_id=${encodeURIComponent(userId)}&limit=${limit}`,
    ),

  fleetSelfReviewTrends: (userId: string, days: number = 30) =>
    getJSON<FleetSelfReviewTrendsDTO>(
      `/api/fleet-self-review/trends?user_id=${encodeURIComponent(userId)}&days=${days}`,
    ),

  fleetSelfReviewRun: (userId: string) =>
    postJSON<FleetSelfReviewDTO>(
      `/api/fleet-self-review/run?user_id=${encodeURIComponent(userId)}`,
      {},
    ),

  // ----------------------------------------------------------------------
  // EX2 — anomaly-detection reports (migration 0038). Fires from the
  // expense ingest path on Discount Bank statements AND from the daily
  // brief loop as a backstop. Home banner consumes /latest; viewer
  // consumes /{id}.
  // ----------------------------------------------------------------------

  anomalyLatest: (userId: string) =>
    getJSON<AnomalyReportDTO | null>(
      `/api/anomalies/latest?user_id=${encodeURIComponent(userId)}`,
    ),

  anomalyById: (userId: string, id: number) =>
    getJSON<AnomalyReportDTO>(
      `/api/anomalies/${id}?user_id=${encodeURIComponent(userId)}`,
    ),
};

// ----------------------------------------------------------------------
// Fleet self-review DTOs
// ----------------------------------------------------------------------

export interface FleetSelfReviewFinding {
  id: string;
  detector: string;
  severity: "RED" | "AMBER" | "YELLOW";
  category: string;
  title: string;
  evidence: Record<string, unknown>;
  suggested_fix: string;
}

export interface FleetSelfReviewDTO {
  id: number;
  user_id: string;
  generated_at: string;
  scope_kind: "post_synthesis" | "daily" | "manual";
  decision_run_id: number | null;
  content_md: string;
  findings: FleetSelfReviewFinding[];
  severity_summary: { RED?: number; AMBER?: number; YELLOW?: number };
}

/** Row shape returned by /api/fleet-self-review/list — lightweight
 *  summary (no markdown body) used by the list page. */
export interface FleetSelfReviewListItemDTO {
  id: number;
  generated_at: string;
  scope_kind: "post_synthesis" | "daily" | "manual";
  decision_run_id: number | null;
  severity_summary: { RED?: number; AMBER?: number; YELLOW?: number };
  findings_total: number;
}

/** One point on the severity-over-time chart. */
export interface FleetSelfReviewTrendPointDTO {
  id: number;
  generated_at: string;
  red: number;
  amber: number;
  yellow: number;
}

/** Response shape of /api/fleet-self-review/trends. */
export interface FleetSelfReviewTrendsDTO {
  points: FleetSelfReviewTrendPointDTO[];
  days: number;
  report_count: number;
  most_persistent_findings: string[];
}

// ----------------------------------------------------------------------
// EX2 anomaly-detection DTOs
// ----------------------------------------------------------------------

/** One detected anomaly from the EX2 watchlist runner. */
export interface AnomalyItem {
  severity: "RED" | "AMBER" | "YELLOW";
  watchlist_entry_name: string;
  observation: string;
  last_seen: string;
  suggested_action: string;
}

/** Per-watchlist-entry state snapshot from one runner pass. */
export interface AnomalyWatchlistStatus {
  name: string;
  state: "NORMAL" | "ALERT" | "RESOLVED" | "UNKNOWN";
  last_evidence: string;
}

/** Parsed ``report_json`` payload — shape of AnomalyDetectionReport. */
export interface AnomalyReportPayload {
  anomalies: AnomalyItem[];
  watchlist_status: AnomalyWatchlistStatus[];
  cited_sources?: string[];
  confidence?: string;
  /** Set when the runner caught an exception while talking to the agent. */
  _runner_error?: string;
}

/** Wire shape for /api/anomalies/latest and /api/anomalies/{id}. */
export interface AnomalyReportDTO {
  id: number;
  user_id: string;
  triggered_by: "event" | "daily" | "manual";
  triggered_at: string;
  source_statement_id: number | null;
  report: AnomalyReportPayload;
  severity_summary: { RED?: number; AMBER?: number; YELLOW?: number };
  agent_report_id: number | null;
}

// ----------------------------------------------------------------------
// Phase 7 type definitions
// ----------------------------------------------------------------------

export interface DomainKbTreeNode {
  name: string;
  path: string;
  is_dir: boolean;
  children: DomainKbTreeNode[];
}

export interface DomainKbFileResponse {
  path: string;
  frontmatter: string;
  content: string;
  raw: string;
}

export interface DomainKbReviewItem {
  id: number;
  path: string;
  diff: string;
  evidence: Array<Record<string, unknown>>;
  status: string;
  note: string;
}

export interface DomainKbReviewQueueResponse {
  rows: DomainKbReviewItem[];
  total: number;
}

export interface IntakeStatusResponse {
  user_id: string;
  user_exists: boolean;
  current_stage: string;
}

export interface IntakeTurnResponse {
  stage: string;
  question_for_user: string;
  stage_complete: boolean;
  next_stage: string | null;
  confidence: string;
  cited_sources: string[];
  notes_for_orchestrator: string;
  context_updates: Array<Record<string, unknown>>;
}

export interface IntakeUploadResponse {
  plan_version_id: number;
  intake_session_id: string;
  fields_extracted: string[];
  fields_missing: string[];
  confidence: string;
  notes: string;
  summary_for_user: string;
}

export interface FileToTextResponse {
  filename: string;
  content_type: string;
  extracted_text: string;
  warnings: string[];
  page_or_sheet_count: number;
}

// ----------------------------------------------------------------------
// Phase 1 reframe: Advisor types
// ----------------------------------------------------------------------

export interface AdvisorTurnResponse {
  stage: string;
  question_for_user: string;
  stage_complete: boolean;
  next_stage: string | null;
  confidence: string;
  cited_sources: string[];
  notes_for_orchestrator: string;
  context_updates: Array<Record<string, unknown>>;
  intake_session_id: string;
  mode: string;
  amendment?: AmendmentResultDTO | null;
}

export type GapState = "fresh" | "missing" | "stale";

export interface AdvisorGapItem {
  path: string;
  label: string;
  section: "identity" | "goals" | "constraints";
  freshness: "one_shot" | "monthly" | "quarterly" | "annual";
  priority: number;
  state: GapState;
  last_updated: string | null;
}

export interface AdvisorGapsResponse {
  user_id: string;
  current_stage: string;
  items: AdvisorGapItem[];
  counts: { fresh: number; missing: number; stale: number };
}

export type AdvisorBriefBulletKind =
  | "draft_plan"
  | "gap"
  | "portfolio"
  | "signal";

export interface AdvisorBriefBullet {
  kind: AdvisorBriefBulletKind;
  text: string;
}

export interface AdvisorBriefCTA {
  label: string;
  href: string;
}

export interface AdvisorHomeBriefResponse {
  headline: string;
  bullets: AdvisorBriefBullet[];
  cta: AdvisorBriefCTA;
  generated_at: string;
}

// ----------------------------------------------------------------------
// Wave 1: Baseline distillate types
// ----------------------------------------------------------------------

export interface DistillateGoal {
  label: string;
  value: string;
  rationale: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface DistillatePrinciple {
  label: string;
  rationale: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface DistillateDecisionRule {
  label: string;
  rule: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface DistillateTarget {
  label: string;
  value: number;
  unit: string;
  stated_at: string; // ISO date
  revisit_after: string; // ISO date
  rationale: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface DistillateConstraint {
  label: string;
  detail: string;
  source_section: string;
  user_edited: boolean;
  user_edit_note: string | null;
}

export interface PlanDistillate {
  plan_label: string;
  distilled_at_iso: string;
  goals: DistillateGoal[];
  principles: DistillatePrinciple[];
  risk_priorities: string[];
  decision_rules: DistillateDecisionRule[];
  targets: DistillateTarget[];
  constraints: DistillateConstraint[];
  stress_tolerance: string;
}

export interface BaselineResponse {
  plan_version_id: number;
  version_label: string;
  raw_markdown: string;
  distillate: PlanDistillate | null;
  distillate_rendered: string | null;
  distilled_at: string | null;
  source_hash: string | null;
}

// ----------------------------------------------------------------------
// Wave 2: synthesis flow + draft lifecycle
// ----------------------------------------------------------------------

export interface DeltaItem {
  item_kind: "target" | "theme" | "action" | "speculative_candidate";
  item_id: string;
  horizon: "long" | "medium" | "short";
  change_kind: "added" | "removed" | "modified";
  summary: string;
  prior: Record<string, unknown> | null;
  proposed: Record<string, unknown> | null;
  rationale: string;
  cited_sources: string[];
  accepted: boolean;
  user_edited: boolean;
  user_edit_note: string | null;
  // Backend-derived (see plan.py::_citation_to_provenance_label).
  // Maps each citation prefix to a human-readable agent/source label,
  // deduplicated in encounter order.
  provenance_agent_labels?: string[];
}

export interface FMObjectionTranslation {
  headline: string;
  plain_english: string;
  recommended_actions: string[];
}

export interface FMObjection {
  severity: "RED" | "AMBER" | "YELLOW";
  topic: string;
  detail: string;
  // Precomputed by the backend on first draft load (cached in
  // fm_objection_translations) and returned inline so the UI toggle
  // between "original Fund Manager wording" and "plain English" is
  // instant — no per-click API round-trip. Null when the translator
  // agent failed; the UI falls back to the on-demand button which
  // POSTs to /api/plan/draft/objections/translate.
  translation?: FMObjectionTranslation | null;
}

export interface FMObjectionsResponse {
  approved: boolean;
  objections: FMObjection[];
  cited_sources: string[];
  decision_run_id: number | null;
  raw_response_excerpt: string;
  // Prior-round FM objections — present when the current draft has a
  // ``role='superseded'`` predecessor with its own FM verdict. Empty
  // list when there's no predecessor / no FM row to fetch. The order
  // matches the prior verdict's ``reasons`` array so the UI can map
  // "Blocker #N" / "Objection #N" tokens in the new delta rationales
  // directly to ``prior_round_objections[N-1]``.
  prior_round_objections?: FMObjection[];
}

// Per-FM-objection stance map returned by GET
// /api/plan/draft/objections/state. Keys are objection_index as a
// string (JSON object keys are strings); missing keys are implicitly
// DEFER. ``plan_version_id`` is echoed back so the UI can pass it to
// PUT/POST without a separate /api/plan/draft round-trip.
export type FMObjectionStance = "AGREE" | "DISAGREE" | "DEFER";

export interface FMObjectionStateRow {
  stance: FMObjectionStance;
  counter_position: string | null;
}

export interface FMObjectionStateMapResponse {
  states: Record<string, FMObjectionStateRow>;
  plan_version_id: number;
}

// FM-objection ZigZag (T4.9) — per-objection slim dialogue between the
// Fund Manager and one analyst. The dialogue runs background-threaded
// on the backend (~30-60 s end-to-end) and produces a structured
// resolution from the FM's final-verdict turn.
export type FMObjectionDialogueResolution =
  | "FM_ACCEPTS_ANALYST"
  | "FM_MAINTAINS_OBJECTION"
  | "FM_REVISES_OBJECTION"
  | "ESCALATE_TO_USER";

export type FMObjectionAnalystStance = "CONCEDE" | "REBUT" | "CLARIFY";

export interface FMObjectionDiscussResponse {
  // "dialogue_started" — fresh background run kicked off.
  // "dialogue_inflight" — idempotency short-circuit; existing run returned.
  // "cost_cap_refused" — 24h spend would breach the cap; no run created.
  status:
    | "dialogue_started"
    | "dialogue_inflight"
    | "cost_cap_refused";
  decision_run_id: number | null;
  inflight: boolean;
  // For "dialogue_started" / "_inflight", carries the analyst's canonical
  // class name (e.g. "TechnicalAnalystAgent") so the UI can echo it back
  // in the "Dialogue in progress with X" status line. For
  // "cost_cap_refused" carries the human-readable cap message.
  detail: string | null;
}

export interface FMObjectionDialogueRow {
  decision_run_id: number;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  objection_index: number;
  analyst_role: string;
  resolution: FMObjectionDialogueResolution | null;
  analyst_stance: FMObjectionAnalystStance | null;
  analyst_reasoning_md: string | null;
  analyst_suggested_fix: string | null;
  fm_reasoning_md: string | null;
  updated_objection_text: string | null;
  suggested_plan_amendment: string | null;
  cited_sources: string[];
}

export interface FMObjectionDialoguesResponse {
  objection_index: number;
  plan_version_id: number;
  dialogues: FMObjectionDialogueRow[];
}

export interface NvdaVestEvent {
  date: string;
  shares: number;
  note: string;
}

export interface NvdaSaleEvent {
  date: string; // YYYY-MM
  shares: number;
  price_usd: number | null;
}

export interface NvdaTrajectoryResponse {
  today_date: string;
  today_shares: number | null;
  vests: NvdaVestEvent[];
  past_sales: NvdaSaleEvent[];
  reduction_program: {
    remaining: number | null;
    sold_ytd: number | null;
    target: number | null;
    progress_pct: number | null;
  };
  ceiling_target_shares: number | null;
  ceiling_target_label: string | null;
}

export interface CashflowPoint {
  months_out: number;
  age_years: number;
  date: string; // YYYY-MM
  portfolio_value_base_usd: number;
  portfolio_value_bear_usd: number;
  portfolio_value_bull_usd: number;
  portfolio_income_base_monthly_usd: number;
  portfolio_income_bear_monthly_usd: number;
  portfolio_income_bull_monthly_usd: number;
  pension_annuity_monthly_usd: number;
  pension_lump_available_usd: number;
  expenses_monthly_usd: number;
  surplus_base_monthly_usd: number;
  surplus_bear_monthly_usd: number;
  surplus_bull_monthly_usd: number;
}

export interface CashflowProjectionResponse {
  today_date: string;
  today_age_years: number;
  fx_usd_nis: number;
  retirement_age_assumed: number;
  retire_ready_age_base: number | null;
  retire_ready_age_bear: number | null;
  retire_ready_age_bull: number | null;
  retire_ready_months_out_base: number | null;
  retire_ready_months_out_bear: number | null;
  retire_ready_months_out_bull: number | null;
  series: CashflowPoint[];
  assumptions: {
    mu_nominal_annual: number;
    sigma_annual: number;
    real_return_annual: number;
    inflation_annual: number;
    mekadem: number;
    tax_rate: number;
    lifestyle_drift_annual: number;       // NEW
    effective_expense_growth: number;     // NEW
    lump_pension_age: number;
    annuity_age: number;
    model_notes: string;
  };
}

export interface MonteCarloPoint {
  months_out: number;
  age_years: number;
  date: string;
  portfolio_value_p10_usd: number;
  portfolio_value_p25_usd: number;
  portfolio_value_p50_usd: number;
  portfolio_value_p75_usd: number;
  portfolio_value_p90_usd: number;
  fraction_solvent: number;
  pension_annuity_monthly_usd: number;
  expenses_monthly_usd: number;
}

export interface MonteCarloProjectionResponse {
  today_date: string;
  today_age_years: number;
  fx_usd_nis: number;
  retirement_age_assumed: number;
  n_paths: number;
  p_failure_before_age_75: number;
  p_failure_before_age_85: number;
  p_failure_before_age_95: number;
  series: MonteCarloPoint[];
  assumptions: {
    mu_nominal_annual: number;
    sigma_annual: number;
    real_return_annual: number;
    inflation_annual: number;
    mekadem: number;
    tax_rate: number;
    lifestyle_drift_annual: number;
    effective_expense_growth: number;
    lump_pension_age: number;
    annuity_age: number;
    n_paths: number;
    model_notes: string;
  };
}

export interface TargetProgress {
  item_id: string;
  target_value: number;
  target_unit: string;
  current_value: number | null;
  current_unit: string;
  gap_value: number | null;
  gap_pct: number | null;
  status: "AT_TARGET" | "ABOVE_TARGET" | "BELOW_TARGET" | "UNKNOWN";
  direction_is_good: boolean | null;
  compute_source: string;
  last_observation: string;
}

export interface TargetProgressResponse {
  plan_version_id: number;
  progress: Record<string, TargetProgress>;
}

export interface DecisionRunRequest {
  user_id: string;
  ticker: string;
  tier: "auto" | "T0" | "T1" | "T2" | "T3";
  analyst_report_ids?: number[];
  positions_summary?: string;
  user_constraints?: string;
  account_class?: "main" | "limited";
  proposed_value_usd?: number;
  portfolio_value_usd?: number;
  account_value_usd?: number;
  is_plan_structural?: boolean;
  crosses_concentration_cap?: boolean;
  recent_red_flag?: boolean;
}

export interface DecisionRunResponse {
  decision_run_id: number;
  status: "approved" | "blocked";
  proposal_id: number | null;
  blocked_reason: string | null;
  blocked_by: string | null;
  tier: string;
}

/**
 * Bounded-risk speculative idea surfaced in the short-horizon plan.
 *
 * Mirrors the python pydantic model
 * ``argosy.agents.plan_synthesizer_types.SpeculativeCandidate`` —
 * keep the two in sync when adding fields.
 */
export interface SpeculativeCandidate {
  ticker: string;
  thesis_summary: string;
  suggested_position_usd: number;
  suggested_position_pct_of_net_worth: number;
  risk_ceiling_check: boolean;
  horizon_days: number;
  expected_drawdown_pct: number;
  exit_trigger: string;
  sourced_from: string[];
}

export interface HorizonView {
  horizon: "long" | "medium" | "short";
  freshness_expected: "annual" | "quarterly" | "monthly";
  status: "no_change" | "minor_revision" | "major_revision";
  posture: string;
  targets: Array<Record<string, unknown>>;
  themes: Array<Record<string, unknown>>;
  actions: Array<Record<string, unknown>>;
  speculative_candidates: SpeculativeCandidate[];
  deltas_from_prior: DeltaItem[];
  rationale: string;
  cited_sources: string[];
}

/**
 * Aggregate per-run agent + adapter health summary (T0.7).
 *
 * Mirrors the backend's ``argosy.api.routes.plan.SynthesisHealth`` model,
 * itself derived from ``build_agent_tree(...).status_summary``. Surfaces
 * the underlying decision_run_id so the UI can deep-link to
 * ``/decisions/{decision_run_id}`` from the banner.
 */
export interface SynthesisHealth {
  agents_ok: number;
  agents_failed: number;
  // "skipped" agents (didn't run at all, e.g. codex zigzag wasn't
  // triggered) are tracked separately from "failed" (ran but errored or
  // returned low confidence).
  agents_skipped: number;
  adapters_ok: number;
  adapters_failed: number;
  decision_run_id: number;
}

/**
 * NVDA divestment-pace snapshot lifted from the latest concentration
 * agent_report tied to the draft's decision_run_id. Mirrors the backend's
 * ``argosy.api.routes.plan.NvdaPaceView`` (itself a thin DTO around
 * ``argosy.agents.concentration_analyst.NvdaPace``).
 *
 * The home page's NVDA PACE tile reads ``shares_sold_ytd`` directly; the
 * displayed annual target stays the UI-side ``NVDA_TARGET_2026`` constant
 * because ``target_shares_ytd`` is the YTD pro-rated number, not the cap.
 * ``on_track`` is authoritative — prefer it over the UI's prior heuristic
 * (pct-of-target vs pct-of-year) when both are available.
 */
export interface NvdaPaceDTO {
  shares_sold_ytd: number;
  target_shares_ytd: number;
  delta_shares: number;
  on_track: boolean;
}

/**
 * Live snapshot of an in-flight plan synthesis run (mirrors backend
 * ``argosy.api.routes.plan.InFlightSynthesisDTO``).
 *
 * Surfaced by ``GET /api/plan/in-flight-synthesis`` so the /plan page
 * can render a "Synthesis #N · phase X of 5" card while a synthesis is
 * mid-flight — useful when the run was triggered outside the UI and the
 * /api/plan/draft endpoint 404s because the prior draft was superseded.
 *
 * ``decision_audit_token`` always shapes as ``plan-synth-<id>`` so the UI
 * can drop it straight into the ``AgentCascadePanel`` filter + the
 * ``/decisions/<id>`` drill-in link.
 */
export interface InFlightSynthesisDTO {
  decision_run_id: number;
  decision_audit_token: string;
  started_at: string;
  completed_phases: number;
  total_phases: number;
  status: string;
}

export interface InFlightSynthesisResponse {
  in_flight_synthesis: InFlightSynthesisDTO | null;
}

/**
 * Home-page action-items widget DTOs — see backend
 * ``argosy.api.routes.plan.ActionItem`` / ``ActionItemsResponse``.
 *
 * Sourced from the user's pending draft (or current accepted plan when
 * no draft exists) by walking horizon_short_json + horizon_medium_json
 * ``actions[]`` and keeping those with a parseable ISO date. ``status``
 * is server-classified by comparing ``dated`` to today's date.
 */
export type ActionItemStatus = "UPCOMING" | "DUE_SOON" | "OVERDUE" | "TODAY";

export interface ActionItem {
  item_id: string;
  horizon: "short" | "medium" | "long";
  label: string;
  detail: string;
  dated: string | null; // ISO YYYY-MM-DD
  days_until: number | null;
  status: ActionItemStatus;
  rationale: string;
  cited_sources: string[];
  plan_version_id: number;
}

export interface ActionItemsResponse {
  items: ActionItem[];
  next_due: string | null;
  overdue_count: number;
  today_count: number;
  upcoming_count: number;
}

export interface DraftResponse {
  plan_version_id: number;
  version_label: string | null;
  drafted_at: string;
  derived_from_id: number | null;
  decision_run_id: number | null;
  horizon_long: HorizonView | null;
  horizon_medium: HorizonView | null;
  horizon_short: HorizonView | null;
  horizon_long_md: string | null;
  horizon_medium_md: string | null;
  horizon_short_md: string | null;
  // T0.7 — derived from build_agent_tree's status_summary on the draft's
  // backing decision_run. Null for drafts without decision_run_id or when
  // the agent-tree builder rejected the run; the SynthesisHealthBanner
  // simply doesn't render in that case.
  synthesis_health?: SynthesisHealth | null;
  // Latest concentration agent_report's nvda_pace block; null when no
  // synthesis has run for this draft or the report is missing/malformed.
  // The home page's NVDA PACE tile renders an "Awaiting synthesis run"
  // tooltip in the null case.
  nvda_pace?: NvdaPaceDTO | null;
}

// ----------------------------------------------------------------------
// Wave 4: plan amendment chat flow
// ----------------------------------------------------------------------

export interface AmendmentResultDTO {
  tier: "small" | "medium" | "large";
  decision_run_id: number;
  status: "applied" | "running" | "needs_confirmation" | "cancelled_existing";
  draft_id: number | null;
  eta_seconds: number | null;
}

export interface AmendmentEventPayload {
  user_id: string;
  decision_run_id: number;
  tier: "small" | "medium" | "large";
  draft_id?: number;
  eta_seconds?: number;
  error?: string;
}

// ----------------------------------------------------------------------
// Wealth Dashboard DTOs — wire-shape mirrors the backend service
// dataclasses in argosy/services/wealth_dashboard.py. Keep these in sync.
// ----------------------------------------------------------------------

export interface WealthScenarioCard {
  name: "bear" | "conservative" | "typical";
  real_return: number;
  years_to_target: number | null;
  target_age: number | null;
  target_portfolio_nis: number | null;
}

export interface WealthTrajectoryPoint {
  year: number;
  bear: number;
  conservative: number;
  typical: number;
}

export interface WealthRetirementBlock {
  net_worth_nis: number | null;
  net_worth_usd: number | null;
  monthly_burn_nis: number | null;
  monthly_income_nis: number | null;
  monthly_surplus_nis: number | null;
  annual_expenses_nis: number | null;
  target_portfolio_nis: number | null;
  swr_rate: number;
  current_age: number;
  current_age_inferred: boolean;
  scenarios: WealthScenarioCard[];
  trajectory: WealthTrajectoryPoint[];
  missing_reasons: string[];
}

export interface WealthCashRunwayBlock {
  cash_nis: number | null;
  sgov_nis: number | null;
  defensive_total_nis: number | null;
  months_of_runway: number | null;
  missing_reasons: string[];
}

export interface WealthConcentrationBlock {
  symbol: string;
  current_pct: number | null;
  target_pct: number | null;
  target_source: string | null;
  missing_reasons: string[];
}

export interface WealthSavingsRateBlock {
  monthly_income_nis: number | null;
  monthly_burn_nis: number | null;
  rate_pct: number | null;
  missing_reasons: string[];
}

export interface WealthFxBucket {
  currency: string;
  value_nis: number;
  pct: number;
}

export interface WealthFxExposureBlock {
  buckets: WealthFxBucket[];
  usd_pct: number | null;
  missing_reasons: string[];
}

export interface WealthRsuQuarter {
  period: string;
  date: string;
  shares: number;
  value_nis: number;
}

export interface WealthRsuIncomeBlock {
  next_12_months_nis: number | null;
  quarters: WealthRsuQuarter[];
  nvda_price_usd: number | null;
  fx_usd_nis: number | null;
  missing_reasons: string[];
}

export interface WealthEstateExposureBlock {
  us_situs_usd: number | null;
  us_situs_nis: number | null;
  nra_exemption_usd: number;
  above_exemption_usd: number | null;
  potential_liability_usd: number | null;
  potential_liability_nis: number | null;
  missing_reasons: string[];
}

export interface WealthCompositionSlice {
  name: string;
  value_nis: number;
  pct: number;
  holdings: string[];
}

export interface WealthAssumptions {
  swr_rate: number;
  scenario_returns: Record<string, number>;
  fx_usd_nis: number | null;
  fx_source: string;
  current_age: number;
  current_age_source: string;
  nvda_target_pct: number | null;
  nvda_target_source: string | null;
  snapshot_date: string | null;
  plan_version_id: number | null;
}

export interface WealthDashboardDTO {
  user_id: string;
  generated_at: string;
  retirement: WealthRetirementBlock;
  cash_runway: WealthCashRunwayBlock;
  concentration: WealthConcentrationBlock;
  savings_rate: WealthSavingsRateBlock;
  fx_exposure: WealthFxExposureBlock;
  rsu_income: WealthRsuIncomeBlock;
  estate_exposure: WealthEstateExposureBlock;
  asset_class_composition: WealthCompositionSlice[];
  sector_composition: WealthCompositionSlice[];
  assumptions: WealthAssumptions;
}

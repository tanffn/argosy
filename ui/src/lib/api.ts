/**
 * Thin fetch helpers for the Argosy backend.
 *
 * The dev Next.js rewrites `/api/*` → `http://localhost:8000/*`, so all
 * calls are relative URLs. In production we expect the same proxy
 * arrangement.
 */

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
}

export interface AgentActivityResponse {
  rows: AgentActivityRow[];
  next_since: string | null;
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

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
  return (await res.json()) as T;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${path}`);
  return (await res.json()) as T;
}

export const api = {
  portfolioSnapshot: (userId: string) =>
    getJSON<PortfolioSnapshotDTO>(
      `/api/portfolio/snapshot?user_id=${encodeURIComponent(userId)}`,
    ),
  planCurrent: (userId: string) =>
    getJSON<PlanCurrentDTO>(
      `/api/plan/current?user_id=${encodeURIComponent(userId)}`,
    ),
  recritique: (userId: string) =>
    postJSON<{ status: string; critique_id: number | null; detail: string }>(
      "/api/plan/critique",
      { user_id: userId },
    ),
  dailyBriefLatest: (userId: string) =>
    getJSON<DailyBriefDTO | null>(
      `/api/daily-brief/latest?user_id=${encodeURIComponent(userId)}`,
    ),
  agentActivity: (userId: string, limit = 10) =>
    getJSON<AgentActivityResponse>(
      `/api/agent-activity?user_id=${encodeURIComponent(userId)}&limit=${limit}`,
    ),
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
};

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

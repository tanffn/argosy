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
};

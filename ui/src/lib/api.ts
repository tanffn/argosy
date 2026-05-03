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
};

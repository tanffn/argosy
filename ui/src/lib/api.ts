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
  cache_input_tokens: number;
  cache_creation_tokens: number;
  thinking_tokens: number;
  citations_count: number;
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
      });
    }
    const fd = new FormData();
    fd.append("user_id", userId);
    fd.append("last_user_message", lastUserMessage);
    if (opts?.currentStage) fd.append("current_stage", opts.currentStage);
    if (opts?.targetField) fd.append("target_field", opts.targetField);
    fd.append("history_excerpt", opts?.historyExcerpt ?? "");
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
    postJSON<{ status: string; decision_run_id: number; draft_id: number }>(
      `/api/advisor/check-in`,
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
  // Wave 4: plan amendment chat flow
  // ----------------------------------------------------------------------

  advisorAmendmentCancel: (userId: string, decisionRunId: number) =>
    postJSON<{ status: string; decision_run_id: number }>(
      `/api/advisor/amendment/${decisionRunId}/cancel?user_id=${encodeURIComponent(userId)}`,
      {},
    ),
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

export interface DraftResponse {
  plan_version_id: number;
  drafted_at: string;
  derived_from_id: number | null;
  decision_run_id: number | null;
  horizon_long: HorizonView | null;
  horizon_medium: HorizonView | null;
  horizon_short: HorizonView | null;
  horizon_long_md: string | null;
  horizon_medium_md: string | null;
  horizon_short_md: string | null;
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

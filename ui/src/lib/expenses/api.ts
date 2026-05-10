/**
 * Expenses API client. Extends @/lib/api with the EX4 endpoints.
 */

const BASE =
  typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_URL
    ? process.env.NEXT_PUBLIC_API_URL
    : "http://localhost:8000";

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return (await res.json()) as T;
}

async function patchJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return (await res.json()) as T;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`);
  return (await res.json()) as T;
}

export interface MonthlyTotalEntry {
  month: string;
  totals_by_currency: Record<string, number>;
  transaction_count: number;
}

export interface CategorySpend {
  slug: string;
  label_en: string;
  total_nis: number;
  transaction_count: number;
  percent: number;
}

export interface MerchantSpend {
  merchant_normalized: string;
  merchant_display: string;
  total_nis: number;
  transaction_count: number;
  category_slug: string | null;
}

export interface AnomalyCard {
  kind:
    | "uncategorized"
    | "novel_merchant"
    | "large_outlier"
    | "fee_waiver_missed"
    | "conservation_gap"
    | "merchant_spike"
    | "new_high_value_merchant";
  severity: "red" | "yellow" | "info";
  message: string;
  detail?: string | null;
  link?: string | null;
}

export interface SourceHealthEntry {
  source_id: number;
  display_name: string;
  issuer: string;
  external_id: string;
  last_period: string | null;
  parsed_total_nis: number | null;
  declared_total_nis: number | null;
  gap: number | null;
  status: "green" | "yellow" | "red" | "unknown";
  statement_count: number;
  correlated_card_payments: number;
}

export interface YearlySummary {
  months_covered: number;
  total_nis: number;                   // deprecated alias for yearly_spending_total_nis
  yearly_spending_total_nis: number;
  yearly_inflow_total_nis: number;
  avg_per_month_nis: number;
  top_categories_12m: CategorySpend[];
  current_vs_avg_pct: number | null;
}

export interface DividendsSummary {
  month: string;
  current_month_total_usd: number;
  yearly_total_usd: number;
  monthly_series: { month: string; total_usd: number }[];
  transactions: TransactionOut[];
}

export interface TaxesSummary {
  yearly_total_nis: number;
  yearly_total_usd: number;
  by_kind: Record<string, number>;
}

export interface DashboardOverview {
  months: MonthlyTotalEntry[];
  current_month: string | null;        // 'YYYY-MM' the headline scopes to
  current_month_spending_nis: number;
  current_month_inflow_nis: number;
  current_month_top_categories: CategorySpend[];
  current_month_inflow: CategorySpend[];
  top_merchants_current_month: MerchantSpend[];
  anomalies: AnomalyCard[];
  sources_health: SourceHealthEntry[];
  yearly_summary: YearlySummary;
  dividends: DividendsSummary;
  taxes: TaxesSummary;
  fx_mode: string;
}

export interface SourceOut {
  id: number;
  kind: string;
  issuer: string;
  external_id: string;
  display_name: string;
  cardholder_name: string | null;
  active: boolean;
}

export interface StatementSummary {
  id: number;
  period_start: string;
  period_end: string;
  parsed_total_nis: number | null;
  declared_total_nis: number | null;
  gap: number | null;
  status: "green" | "yellow" | "red" | "unknown";
  parser_name: string;
  parser_version: string;
  transaction_count: number;
  correlated_count: number;
}

export interface MonthBucket {
  month: string;                       // 'YYYY-MM'
  debit_nis: number;
  credit_nis: number;
  transaction_count: number;
  correlated_count: number;
}

export interface SourceDetailResponse {
  source: SourceOut;
  statements: StatementSummary[];
  months: MonthBucket[];
}

export interface TransactionOut {
  id: number;
  occurred_on: string;
  merchant_raw: string;
  amount_nis: number | null;
  amount_orig: number | null;
  currency_orig: string | null;
  direction: "debit" | "credit";
  tx_type: string;
  category_slug: string | null;
  category_source: string | null;
  is_card_payment: boolean;
  source_id: number;
}

export interface TransactionsResponse {
  transactions: TransactionOut[];
  total: number;
}

export interface CategoryOut {
  id: number;
  slug: string;
  label_en: string;
  label_he: string;
  parent_slug: string | null;
  is_excluded_from_spend: boolean;
  is_inflow: boolean;
}

export interface CategoriesResponse {
  categories: CategoryOut[];
}

export interface SourcesResponse {
  sources: SourceOut[];
}

export const expensesApi = {
  dashboardOverview: (
    userId: string,
    months = 12,
    fx: "per_currency" | "nis" = "per_currency",
    month?: string | null,
  ) => {
    const qs = new URLSearchParams({
      user_id: userId,
      months: String(months),
      fx,
    });
    if (month) qs.set("month", month);
    return getJSON<DashboardOverview>(
      `/api/expenses/dashboard-overview?${qs.toString()}`,
    );
  },
  sources: (userId: string) =>
    getJSON<SourcesResponse>(
      `/api/expenses/sources?user_id=${encodeURIComponent(userId)}`,
    ),
  sourceDetail: (sourceId: number, userId: string) =>
    getJSON<SourceDetailResponse>(
      `/api/expenses/source-detail/${sourceId}?user_id=${encodeURIComponent(userId)}`,
    ),
  transactions: (userId: string, params: Partial<{
    from_date: string;
    to_date: string;
    category: string;
    source_id: number;
    direction: "debit" | "credit";
    include_card_payments: boolean;
    search: string;
    tag: string;
    limit: number;
    offset: number;
  }> = {}) => {
    const qs = new URLSearchParams({ user_id: userId });
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "")
        qs.set(k, String(v));
    }
    return getJSON<TransactionsResponse>(`/api/expenses/transactions?${qs.toString()}`);
  },
  categories: (userId: string) =>
    getJSON<CategoriesResponse>(
      `/api/expenses/categories?user_id=${encodeURIComponent(userId)}`,
    ),
  patchTransactionCategory: (txId: number, userId: string, slug: string) =>
    patchJSON<{
      transaction_id: number;
      category_slug: string;
      category_source: string;
      affected_count: number;
    }>(`/api/expenses/transactions/${txId}`, {
      user_id: userId,
      category_slug: slug,
    }),
  // Tags (Feature 3)
  setTags: (txId: number, userId: string, tags: string[]) =>
    patchJSON<{ transaction_id: number; tags: string[] }>(
      `/api/expenses/transactions/${txId}/tags`,
      { user_id: userId, tags },
    ),
  addTag: (txId: number, userId: string, tag: string) =>
    postJSON<{ transaction_id: number; tags: string[] }>(
      `/api/expenses/transactions/${txId}/tags/add`,
      { user_id: userId, tag },
    ),
  removeTag: (txId: number, userId: string, tag: string) =>
    postJSON<{ transaction_id: number; tags: string[] }>(
      `/api/expenses/transactions/${txId}/tags/remove`,
      { user_id: userId, tag },
    ),
  listTags: (userId: string, prefix?: string) => {
    const qs = new URLSearchParams({ user_id: userId });
    if (prefix) qs.set("prefix", prefix);
    return getJSON<{ tags: string[] }>(`/api/expenses/tags?${qs.toString()}`);
  },
  tripSummary: (userId: string, tag: string) =>
    getJSON<TripSummary>(
      `/api/expenses/trip-summary?user_id=${encodeURIComponent(userId)}&tag=${encodeURIComponent(tag)}`,
    ),
};

export interface CurrencyAmount {
  currency: string;
  total: number;
}

export interface TripSummary {
  tag: string;
  transaction_count: number;
  total_nis: number;
  currency_breakdown: CurrencyAmount[];
  by_category: CategorySpend[];
  transactions: TransactionOut[];
  period_start: string | null;
  period_end: string | null;
}

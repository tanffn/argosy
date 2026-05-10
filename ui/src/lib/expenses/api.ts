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

export type YearlyWindow = "trailing_12" | "calendar_year";

export interface YearlySummary {
  months_covered: number;
  total_nis: number;                   // deprecated alias for yearly_spending_total_nis
  yearly_spending_total_nis: number;
  yearly_income_total_nis: number;
  yearly_refunds_total_nis: number;
  yearly_inflow_total_nis: number;     // deprecated alias = income + refunds
  avg_per_month_nis: number;
  top_categories_12m: CategorySpend[]; // ALL spending categories, sorted desc
  current_vs_avg_pct: number | null;
  window: YearlyWindow;
  window_label: string;
  window_start_month: string;
  window_end_month: string;
}

export interface DividendsSummary {
  month: string;
  current_month_total_usd: number;
  yearly_total_usd: number;
  monthly_series: { month: string; total_usd: number }[];
  trend_12mo: TrendPoint[];
  transactions: TransactionOut[];
}

export interface TaxesSummary {
  yearly_total_nis: number;
  yearly_total_usd: number;
  by_kind: Record<string, number>;
  trend_12mo: TrendPoint[];
}

export interface SavingsRatePoint {
  month: string;
  income_nis: number;
  spending_nis: number;
  savings_rate: number;
}

export interface CategoryDelta {
  slug: string;
  label: string;
  current_nis: number;
  prior_nis: number;
  delta_nis: number;
  delta_pct: number | null;
}

export interface TopMovers {
  grew: CategoryDelta[];
  shrank: CategoryDelta[];
  reason: string | null;
}

export interface CurrencyMixPoint {
  month: string;
  nis: number;
  usd: number;
}

export interface TrendPoint {
  month: string;
  total_nis: number;
  total_usd: number;
}

export interface ChartWindowBar {
  month: string;
  total_nis: number;
  total_usd: number;
  is_padding: boolean;
  is_selected: boolean;
}

export interface HeroMetric {
  value_nis: number;
  mom_delta_pct: number | null;
  vs_trailing12_pct: number | null;
}

export interface HeroStatsMonthly {
  spent: HeroMetric;
  income: HeroMetric;
  refunds: HeroMetric;
  statements_reconciled: number;
  anomalies_count: number;
}

export interface CategoryDeviation {
  slug: string;
  label: string;
  this_month_nis: number;
  typical_mean_nis: number;
  typical_std_nis: number;
  z_score: number;
  delta_pct: number | null;
}

export interface DashboardOverview {
  months: MonthlyTotalEntry[];
  yearly_summary: YearlySummary;
  savings_rate_trend: SavingsRatePoint[];
  top_movers: TopMovers;
  currency_mix: CurrencyMixPoint[];
  dividends: DividendsSummary | null;
  taxes: TaxesSummary | null;
  sources_health: SourceHealthEntry[];
  fx_mode: string;
}

export interface DashboardMonthly {
  month: string;
  available_months: string[];
  chart_window: ChartWindowBar[];
  hero_stats: HeroStatsMonthly;
  top_categories: CategorySpend[];
  categories_vs_typical: CategoryDeviation[];
  top_merchants: MerchantSpend[];
  largest_transactions: TransactionOut[];
  anomalies: AnomalyCard[];
  fx_mode: string;
}

export interface IncomeBreakdown {
  month: string;
  total_nis: number;
  by_category: CategorySpend[];
  transactions: TransactionOut[];
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
  tags: string[];
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
    window?: YearlyWindow | null,
  ) => {
    const qs = new URLSearchParams({
      user_id: userId,
      months: String(months),
      fx,
    });
    if (window) qs.set("window", window);
    return getJSON<DashboardOverview>(
      `/api/expenses/dashboard-overview?${qs.toString()}`,
    );
  },
  dashboardMonthly: (
    userId: string,
    month: string,
    fx: "per_currency" | "nis" = "per_currency",
  ) => {
    const qs = new URLSearchParams({ user_id: userId, month, fx });
    return getJSON<DashboardMonthly>(
      `/api/expenses/dashboard-monthly?${qs.toString()}`,
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
  incomeBreakdown: (userId: string, month: string) =>
    getJSON<IncomeBreakdown>(
      `/api/expenses/income-breakdown?user_id=${encodeURIComponent(userId)}&month=${month}`,
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
  rsuReconciliation: (
    userId: string,
    opts: { tolerance_usd?: number; tolerance_days?: number } = {},
  ) => {
    const qs = new URLSearchParams({ user_id: userId });
    if (opts.tolerance_usd !== undefined)
      qs.set("tolerance_usd", String(opts.tolerance_usd));
    if (opts.tolerance_days !== undefined)
      qs.set("tolerance_days", String(opts.tolerance_days));
    return getJSON<RsuReconciliationResponse>(
      `/api/expenses/rsu-reconciliation?${qs.toString()}`,
    );
  },
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

// ---------------------------------------------------------------------------
// RSU reconciliation (Schwab → Leumi USD)
// ---------------------------------------------------------------------------

export interface RsuSaleLot {
  shares: number;
  sale_price_usd: number;
  vest_date: string | null;
  gross_proceeds_usd: number | null;
  cost_basis_usd: number | null;
  realized_gain_usd: number | null;
  taxes_usd: number | null;
  holding_period: string | null;
}

export interface RsuSale {
  date: string;
  symbol: string;
  quantity_shares: number;
  gross_usd: number;
  fees_usd: number;
  net_usd: number;
  total_taxes_usd: number;
  lots: RsuSaleLot[];
}

export interface RsuDisbursement {
  date: string;
  amount_usd: number;
  matched_leumi_credit_id: number | null;
  days_diff: number | null;
  // Signed: positive == bank received less than Schwab disbursed (haircut),
  // negative == bank received more (FX gain), 0 == perfect match.
  amount_diff_usd: number | null;
  match_kind: "exact" | "haircut" | null;
  haircut_pct: number | null;
}

export interface RsuLeumiCredit {
  tx_id: number;
  date: string;
  amount_usd: number;
  merchant_raw: string;
  reference: string | null;
  matched_disbursement_index: number | null;
}

export interface RsuPendingSale {
  date: string;
  quantity_shares: number;
  gross_usd: number;
  net_usd: number;
  days_since_sale: number;
}

export interface RsuSummary {
  sales_count: number;
  sales_total_gross_usd: number;
  sales_total_fees_usd: number;
  sales_total_net_usd: number;
  sales_total_taxes_usd: number;
  disbursements_count: number;
  disbursements_matched_count: number;
  disbursements_total_usd: number;
  leumi_credits_count: number;
  leumi_credits_unmatched_count: number;
  leumi_credits_unmatched_total_usd: number;
  pending_sales_count: number;
  pending_sales_total_gross_usd: number;
}

export interface RsuReconciliationResponse {
  sales: RsuSale[];
  disbursements: RsuDisbursement[];
  leumi_credits: RsuLeumiCredit[];
  pending_sales: RsuPendingSale[];
  summary: RsuSummary;
  schwab_csv_paths: string[];
  warning: string | null;
}

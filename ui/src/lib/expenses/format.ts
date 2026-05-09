/**
 * Formatting helpers for the expenses UI.
 */

const NIS_FORMATTER = new Intl.NumberFormat("en-IL", {
  style: "currency",
  currency: "ILS",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

const NIS_FORMATTER_2DP = new Intl.NumberFormat("en-IL", {
  style: "currency",
  currency: "ILS",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const FOREIGN_FORMATTERS: Record<string, Intl.NumberFormat> = {
  USD: new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }),
  EUR: new Intl.NumberFormat("en-EU", { style: "currency", currency: "EUR" }),
  GBP: new Intl.NumberFormat("en-GB", { style: "currency", currency: "GBP" }),
};

export function formatNIS(amount: number, opts: { precise?: boolean } = {}): string {
  return (opts.precise ? NIS_FORMATTER_2DP : NIS_FORMATTER).format(amount);
}

export function formatCurrency(amount: number, currency: string): string {
  if (currency === "NIS" || currency === "ILS") return formatNIS(amount);
  const fmt = FOREIGN_FORMATTERS[currency];
  if (fmt) return fmt.format(amount);
  return `${amount.toFixed(2)} ${currency}`;
}

export function formatPercent(p: number, decimals = 1): string {
  return `${p.toFixed(decimals)}%`;
}

export function formatMonth(yyyymm: string): string {
  // '2026-05' -> 'May 2026'
  const [y, m] = yyyymm.split("-").map(Number);
  if (!y || !m) return yyyymm;
  const dt = new Date(y, m - 1, 1);
  return dt.toLocaleDateString("en-US", { month: "short", year: "numeric" });
}

export function formatRelativeMonth(yyyymm: string): string {
  const now = new Date();
  const [y, m] = yyyymm.split("-").map(Number);
  if (!y || !m) return yyyymm;
  const target = new Date(y, m - 1, 1);
  const cur = new Date(now.getFullYear(), now.getMonth(), 1);
  const diff = (cur.getFullYear() - target.getFullYear()) * 12 + (cur.getMonth() - target.getMonth());
  if (diff === 0) return "This month";
  if (diff === 1) return "Last month";
  if (diff < 12) return `${diff} months ago`;
  return formatMonth(yyyymm);
}

/**
 * Stable HSL color from a category slug. Same slug → same color across renders.
 */
export function colorForSlug(slug: string): string {
  let h = 0;
  for (let i = 0; i < slug.length; i++) {
    h = (h * 31 + slug.charCodeAt(i)) % 360;
  }
  return `hsl(${h}, 65%, 55%)`;
}

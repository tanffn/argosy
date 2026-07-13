/**
 * Physical (illiquid) real estate vs listed property securities.
 *
 * Matches the backend invariant in
 * ``argosy.services.plan_numeric_resolver._is_real_estate``: a row whose
 * label mentions "real estate" but HAS a tradable ticker (IWDP, O, …) is
 * liquid capital and must appear in account tables + liquid totals. Only
 * direct/illiquid property (symbol ``-`` / empty / n/a) is excluded — those
 * surface in the RealEstateCard from ``real_estate_json``, not ``positions[]``.
 */

import type { PortfolioPosition, PortfolioSnapshotDTO } from "@/lib/api";

const NON_TRADABLE = new Set(["", "-", "—", "n/a", "na", "none"]);

export function isPhysicalRealEstate(p: PortfolioPosition): boolean {
  const blob = `${p.asset_type || ""} ${p.details || ""} ${p.type_label || ""}`.toLowerCase();
  if (!blob.includes("real estate") && !blob.includes("real-estate")) {
    return false;
  }
  const sym = (p.symbol || "").trim().toLowerCase();
  return !sym || NON_TRADABLE.has(sym);
}

export interface AccountGroup {
  location: string;
  positions: PortfolioPosition[];
  total_usd_k: number;
}

/** The NVDA RSU row's location is bare "schwab" while other Schwab holdings
 *  are "schwab 876" — same account, so group them together. */
export function normalizeLocation(loc: string): string {
  const l = (loc || "").trim();
  if (l.toLowerCase() === "schwab") return "schwab 876";
  return l || "(unknown)";
}

/** Group liquid (non-physical-RE) positions by account. */
export function groupByAccount(snap: PortfolioSnapshotDTO | null): AccountGroup[] {
  if (!snap) return [];
  const map = new Map<string, AccountGroup>();
  for (const p of snap.positions) {
    if (isPhysicalRealEstate(p)) continue;
    const key = normalizeLocation(p.location);
    const g = map.get(key) ?? { location: key, positions: [], total_usd_k: 0 };
    g.positions.push(p);
    g.total_usd_k += p.usd_value_k ?? 0;
    map.set(key, g);
  }
  return Array.from(map.values()).sort(
    (a, b) => b.total_usd_k - a.total_usd_k,
  );
}

/**
 * Output-trust invariant: every ``positions[]`` row must render exactly once
 * across the page's sections (account tables XOR physical-RE exclusion for
 * the RealEstateCard path). Listed property securities must never vanish.
 *
 * Returns null when the invariant holds; otherwise an error message.
 */
export function assertPositionsPartition(
  snap: PortfolioSnapshotDTO,
): string | null {
  const inAccounts = new Set<PortfolioPosition>();
  const physical: PortfolioPosition[] = [];
  for (const p of snap.positions) {
    if (isPhysicalRealEstate(p)) physical.push(p);
    else inAccounts.add(p);
  }
  if (inAccounts.size + physical.length !== snap.positions.length) {
    return (
      `positions partition broken: ${inAccounts.size} account + ` +
      `${physical.length} physical-RE ≠ ${snap.positions.length} total`
    );
  }
  // Every listed REIT/property security must be in an account group.
  for (const p of snap.positions) {
    const sym = (p.symbol || "").trim().toUpperCase();
    if (!sym || NON_TRADABLE.has(sym.toLowerCase())) continue;
    const blob = `${p.asset_type || ""}`.toLowerCase();
    if (
      (blob.includes("real estate") || blob.includes("reit")) &&
      isPhysicalRealEstate(p)
    ) {
      return `tradable property security ${sym} wrongly classified as physical RE`;
    }
  }
  const groups = groupByAccount(snap);
  const rendered = groups.reduce((n, g) => n + g.positions.length, 0);
  if (rendered !== inAccounts.size) {
    return (
      `account tables render ${rendered} rows but liquid partition has ` +
      `${inAccounts.size}`
    );
  }
  const liquidSum = groups.reduce((s, g) => s + g.total_usd_k, 0);
  const expectedLiquid = snap.positions
    .filter((p) => !isPhysicalRealEstate(p))
    .reduce((s, p) => s + (p.usd_value_k ?? 0), 0);
  if (Math.abs(liquidSum - expectedLiquid) > 1e-6) {
    return (
      `liquid section totals ${liquidSum}K ≠ sum of liquid positions ` +
      `${expectedLiquid}K`
    );
  }
  return null;
}

/**
 * Mirror of argosy/services/plain_english_labels.py for the UI.
 *
 * Maps internal source_id patterns and agent roles to user-friendly
 * labels so internal config refs don't leak into rendered copy.
 *
 * Keep in sync with the Python version. When adding a new entry,
 * update both sides. The UI side intentionally has no
 * `friendly_agent_role` -- that mapping happens server-side at
 * thesis-generation time (see per_position_thesis.py).
 */

const SOURCE_PREFIX_LABELS: ReadonlyArray<[string, (rest: string) => string]> = [
  ["indicators/", (rest) => `${rest} technical indicators`],
  ["fundamentals:", (rest) => `${rest} fundamentals`],
  ["fundamentals/", (rest) => `${rest} fundamentals`],
  ["news/", (rest) => `${rest} news`],
  ["sentiment/", (rest) => `${rest} sentiment`],
  ["agent_report:", (rest) => `agent report #${rest}`],
  ["fx/", (rest) => `FX ${rest}`],
  ["rates/", (rest) => `FX rate ${rest}`],
  ["macro/", (rest) => `macro ${rest}`],
  ["tax/", (rest) => `tax rules ${rest}`],
  ["policy/", (rest) => `policy ${rest}`],
  ["doc:", (rest) => `doc ${rest}`],
  ["plan_critique:", (rest) => `plan critique ${rest}`],
  ["synth:", (rest) => `synthesis ${rest}`],
];

const DATED_RE = /^([a-z]+):([A-Z]{1,8}):(\d{4}-\d{2}-\d{2})$/;

const DATED_KIND_LABEL: Record<string, string> = {
  fundamentals: "fundamentals",
  news: "news",
  sentiment: "sentiment",
  technical: "technical",
  indicators: "technical indicators",
};

export function friendlySourceLabel(sourceId: string): string {
  if (!sourceId) return "";
  const m = DATED_RE.exec(sourceId);
  if (m) {
    const [, prefix, ticker, dt] = m;
    const kind = DATED_KIND_LABEL[prefix] ?? prefix;
    return `${ticker} ${kind} (${dt})`;
  }
  for (const [prefix, fn] of SOURCE_PREFIX_LABELS) {
    if (sourceId.startsWith(prefix)) {
      const rest = sourceId.slice(prefix.length).replace(/[/:]/g, " ");
      return fn(rest);
    }
  }
  return sourceId;
}

// ---------------------------------------------------------------------------
// Plan-delta item_id -> user-friendly label
// ---------------------------------------------------------------------------

// Acronyms that should stay capitalized when reflowing snake_case.
const ACRONYMS = new Set([
  "us", "usa", "uk", "eu", "il", "ira", "irs", "rsu", "rsus", "etf", "etfs",
  "nis", "usd", "eur", "gbp", "jpy", "nvda", "amd", "qqqm", "schg", "voo",
  "fx", "p10", "p50", "p90", "p99", "p25", "p75", "p95", "ytd", "tase",
  "gemel", "pensia", "hishtalmut", "kupat", "ai", "rmd", "esg",
]);

const HORIZON_PREFIXES = new Set(["long", "medium", "short"]);
const KIND_PREFIXES = new Set([
  "targets", "themes", "actions", "speculative_candidates",
  "speculative", "candidates",
]);

/**
 * Translate a plan-delta item_id into a user-friendly label.
 *
 * Item IDs follow the pattern `<horizon>.<kind>.<semantic_name>` where
 * `<semantic_name>` is snake_case English. The translator:
 *   * Strips the horizon + kind prefix (already shown elsewhere on the card).
 *   * Splits the semantic_name on underscores.
 *   * Re-capitalizes known acronyms.
 *
 * Examples:
 *   long.targets.us_situs_taxable_assets_cap
 *     -> "US situs taxable assets cap"
 *   medium.targets.cash_buffer_months
 *     -> "cash buffer months"
 *   long.targets.nvda_share_of_portfolio_12mo
 *     -> "NVDA share of portfolio 12mo"
 *
 * Unknown shapes fall back to the raw item_id.
 */
export function friendlyItemId(itemId: string): string {
  if (!itemId) return "";
  const parts = itemId.split(".");
  let semantic = itemId;
  if (parts.length >= 3 && HORIZON_PREFIXES.has(parts[0])) {
    semantic = parts.slice(2).join(".");
  } else if (parts.length >= 2 && KIND_PREFIXES.has(parts[0])) {
    semantic = parts.slice(1).join(".");
  }
  // Reflow each dot-separated chunk separately so nested keys stay legible.
  return semantic
    .split(".")
    .map((chunk) =>
      chunk
        .split("_")
        .map((word) => {
          const lower = word.toLowerCase();
          if (ACRONYMS.has(lower)) return lower.toUpperCase();
          return lower;
        })
        .join(" "),
    )
    .join(" · ");
}

export function friendlySourceLabels(sourceIds: string[], maxCount = 6): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const sid of sourceIds) {
    const label = friendlySourceLabel(sid);
    if (label && !seen.has(label)) {
      seen.add(label);
      out.push(label);
      if (out.length >= maxCount) break;
    }
  }
  return out;
}

"use client";

import Link from "next/link";
import { useState } from "react";
import { HelpCircle, Plus, Trash2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type DecisionRunRequest,
  type DecisionRunResponse,
} from "@/lib/api";

const USER_ID = "ariel";

type ActionHint = "buy" | "sell" | "hold" | "lean_buy" | "lean_sell";

interface InputRow {
  id: string;
  ticker: string;
  action: ActionHint;
  rationale: string;
}

interface OutcomeRow {
  ticker: string;
  action: ActionHint;
  rationale: string;
  state: "running" | "ok" | "error";
  result?: DecisionRunResponse;
  error?: string;
}

const ACTIONS: Array<{ value: ActionHint; label: string }> = [
  { value: "buy", label: "Buy" },
  { value: "sell", label: "Sell" },
  { value: "hold", label: "Hold" },
  { value: "lean_buy", label: "Lean buy" },
  { value: "lean_sell", label: "Lean sell" },
];

// /consult is an ad-hoc ticker consultation surface. T0 ("trader only,
// known watchlist ticker, no recent news") and "auto" (which resolves
// to T0 with the consult page's sentinel portfolio_value_usd=1.0) both
// produce a trader run with no analyst sources to cite — see SDD §3.3
// + §4.1. The full-fleet tiers are the meaningful options here; T2
// matches the SDD "9 analysts + 2-round debate + risk team + FM" shape.
const TIERS = ["T1", "T2", "T3"] as const;

// /consult mode (2026-05-31). `long_hold` shifts the analyst set (no
// FX, no technical) AND swaps the trader prompt to a long-horizon
// thesis-fit framing — see argosy/decisions/per_ticker_analysts.py +
// argosy/agents/trader.py. Default `tactical_trade` preserves the
// SDD §3.3 trader behaviour for one-off entry-timing questions.
const MODES = [
  { value: "long_hold", label: "Long hold (5+ year)" },
  { value: "tactical_trade", label: "Tactical trade (entry timing)" },
] as const;
type ConsultMode = (typeof MODES)[number]["value"];

function newRow(): InputRow {
  return {
    id: crypto.randomUUID(),
    ticker: "",
    action: "buy",
    rationale: "",
  };
}

function buildConstraintsText(row: InputRow): string {
  const action = row.action.replace("_", " ");
  return (
    `User submitted an ad-hoc consult request for ticker ${row.ticker.toUpperCase()}. ` +
    `User's stated lean: ${action.toUpperCase()}. ` +
    `User's rationale (verbatim): ${row.rationale.trim() || "(none provided)"}.`
  );
}

function actionVariant(action: ActionHint): "success" | "error" | "secondary" {
  if (action === "buy" || action === "lean_buy") return "success";
  if (action === "sell" || action === "lean_sell") return "error";
  return "secondary";
}

// --- Outcome-card formatting helpers ---------------------------------
//
// The backend returns a `RunResponse` with `status` ∈ {approved, blocked}
// and `blocked_by` ∈ {trader_hold, risk_team, plan_critique_red,
// fund_manager} when blocked. `blocked_reason` is the trader/risk/FM
// rationale_summary — a paragraph of prose with inline source citations
// in `[...]` brackets that look like
//   `[indicators/APD, yfinance:APD:1d, fx/rates/USD/NIS (boi:USD)]`
// We surface those citations as pills and strip them from the body so
// the prose reads cleanly.

type Verdict = "BUY" | "SELL" | "HOLD" | "NEEDS DATA" | "BLOCKED";

interface ParsedOutcome {
  verdict: Verdict;
  verdictTone: "success" | "error" | "warning" | "secondary";
  blockedByLabel: string | null; // e.g. "Trader" / "Risk team" / "Fund manager"
  summary: string; // first sentence, no citations
  body: string; // rest of blocked_reason with citations stripped
  citations: string[]; // unique deduped pill labels
}

const BLOCKED_BY_LABEL: Record<string, string> = {
  trader_hold: "Trader",
  trader_insufficient_data: "Trader (data gap)",
  risk_team: "Risk team",
  plan_critique_red: "Plan critique",
  fund_manager: "Fund manager",
};

function parseCitations(text: string): string[] {
  // Match `[ ... ]` groups that look like citation lists — comma-separated,
  // contain at least one `/`, `:`, or `(`. This avoids snagging on plain
  // bracketed asides in prose like `[sic]`.
  const re = /\[([^\[\]]+)\]/g;
  const seen = new Set<string>();
  const out: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const inner = m[1];
    if (!/[\/:]/.test(inner)) continue; // skip plain bracketed asides
    for (const raw of inner.split(",")) {
      // Drop trailing parenthetical qualifiers like `(boi:USD)` — they're
      // provider hints, not source IDs.
      const cleaned = raw.replace(/\s*\([^)]*\)\s*$/, "").trim();
      if (!cleaned) continue;
      if (seen.has(cleaned)) continue;
      seen.add(cleaned);
      out.push(cleaned);
    }
  }
  return out;
}

function stripCitations(text: string): string {
  // Drop `[...]` groups that look like citation lists (same heuristic as
  // parseCitations). Collapse double spaces left behind.
  return text
    .replace(/\s*\[([^\[\]]+)\]/g, (full, inner: string) =>
      /[\/:]/.test(inner) ? "" : full,
    )
    .replace(/\s{2,}/g, " ")
    .replace(/\s+([.,;:])/g, "$1")
    .trim();
}

function splitFirstSentence(text: string): { first: string; rest: string } {
  if (!text) return { first: "", rest: "" };
  // Strip a leading "Trader returned HOLD:" / "Trader returned BUY:" preamble
  // when present — the verdict is already shown as a heading.
  const preambleRe = /^Trader returned (?:HOLD|BUY|SELL|BLOCKED|INSUFFICIENT_DATA)\s*:\s*/i;
  const stripped = text.replace(preambleRe, "");
  // Find first sentence terminator followed by whitespace. Tolerate
  // missing trailing punctuation.
  const idx = stripped.search(/\.\s+[A-Z]/);
  if (idx === -1) {
    // No clear sentence break — fall back to ~120 chars.
    if (stripped.length <= 140) return { first: stripped, rest: "" };
    const cut = stripped.lastIndexOf(" ", 140);
    return {
      first: stripped.slice(0, cut > 60 ? cut : 140).trimEnd() + "…",
      rest: stripped.slice(cut > 60 ? cut : 140).trim(),
    };
  }
  return {
    first: stripped.slice(0, idx + 1).trim(),
    rest: stripped.slice(idx + 1).trim(),
  };
}

function verdictFor(
  action: ActionHint,
  result: DecisionRunResponse,
): { verdict: Verdict; tone: "success" | "error" | "warning" | "secondary" } {
  if (result.status === "approved") {
    // The trader's actual side comes from the user's stated lean — the
    // fleet either confirms it or pushes back. (The backend doesn't echo
    // the side on RunResponse.)
    const isSell = action === "sell" || action === "lean_sell";
    return isSell
      ? { verdict: "SELL", tone: "error" }
      : { verdict: "BUY", tone: "success" };
  }
  // blocked
  if (result.blocked_by === "trader_hold") {
    return { verdict: "HOLD", tone: "warning" };
  }
  if (result.blocked_by === "trader_insufficient_data") {
    // Distinct from HOLD — analysis couldn't complete because
    // load-bearing data was missing after remediation. The result
    // card shows this as "NEEDS DATA" with a secondary tone so the
    // user knows the system didn't fail-soft into a wait-it-out
    // recommendation.
    return { verdict: "NEEDS DATA", tone: "secondary" };
  }
  return { verdict: "BLOCKED", tone: "error" };
}

function parseOutcome(
  action: ActionHint,
  result: DecisionRunResponse,
): ParsedOutcome {
  const { verdict, tone } = verdictFor(action, result);
  const blockedByLabel =
    result.status === "blocked" && result.blocked_by
      ? BLOCKED_BY_LABEL[result.blocked_by] ?? result.blocked_by
      : null;
  const reason = result.blocked_reason ?? "";
  const citations = parseCitations(reason);
  const cleaned = stripCitations(reason);
  const { first, rest } = splitFirstSentence(cleaned);
  return {
    verdict,
    verdictTone: tone,
    blockedByLabel,
    summary: first,
    body: rest,
    citations,
  };
}

function verdictHeadingClass(
  tone: "success" | "error" | "warning" | "secondary",
): string {
  if (tone === "success") return "text-success";
  if (tone === "error") return "text-error";
  if (tone === "warning") return "text-warning";
  return "text-muted-foreground";
}

export function ConsultRunner() {
  const [rows, setRows] = useState<InputRow[]>([newRow()]);
  const [tier, setTier] = useState<(typeof TIERS)[number]>("T2");
  const [mode, setMode] = useState<ConsultMode>("long_hold");
  const [outcomes, setOutcomes] = useState<OutcomeRow[]>([]);
  const [submitting, setSubmitting] = useState(false);

  const addRow = () => setRows((rs) => [...rs, newRow()]);
  const removeRow = (id: string) =>
    setRows((rs) => (rs.length > 1 ? rs.filter((r) => r.id !== id) : rs));
  const updateRow = (id: string, patch: Partial<InputRow>) =>
    setRows((rs) => rs.map((r) => (r.id === id ? { ...r, ...patch } : r)));

  const canSubmit = rows.every((r) => r.ticker.trim().length > 0) && !submitting;

  const onSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);

    // Snapshot the inputs and immediately render an "outcomes" list with
    // all rows in running state so the user can see progress per-ticker.
    const queued: OutcomeRow[] = rows.map((r) => ({
      ticker: r.ticker.trim().toUpperCase(),
      action: r.action,
      rationale: r.rationale.trim(),
      state: "running",
    }));
    setOutcomes(queued);

    // Fire all decision flows in parallel. Each call is awaited
    // independently so partial successes still surface.
    await Promise.all(
      rows.map(async (r, i) => {
        const body: DecisionRunRequest = {
          user_id: USER_ID,
          ticker: r.ticker.trim().toUpperCase(),
          tier,
          analyst_report_ids: [],
          user_constraints: buildConstraintsText(r),
          // The flow's auto-tier resolver needs SOME portfolio context; ship
          // 1.0 as a sentinel so we don't divide-by-zero. With tier='auto'
          // and no proposed_value, the resolver lands at T0 by default.
          portfolio_value_usd: 1.0,
          consult_mode: mode,
        };
        try {
          const result = await api.decisionsRun(body);
          setOutcomes((prev) => {
            const next = [...prev];
            next[i] = { ...next[i], state: "ok", result };
            return next;
          });
        } catch (e: unknown) {
          setOutcomes((prev) => {
            const next = [...prev];
            next[i] = {
              ...next[i],
              state: "error",
              error: e instanceof Error ? e.message : String(e),
            };
            return next;
          });
        }
      }),
    );

    setSubmitting(false);
  };

  return (
    <div className="flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Consult</h1>
        <p className="text-sm text-muted-foreground">
          Submit one or more tickers with your conviction and rationale. The
          agent fleet runs a decision flow per ticker and returns a Buy / Sell /
          Hold recommendation backed by a full reasoning trail. The actual
          accept/execute happens on <Link href="/proposals" className="text-primary hover:underline">/proposals</Link>.
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Tickers to consult on</CardTitle>
          <CardDescription>
            One row per ticker. Your rationale flows into the analyst prompts
            as user-supplied context so the fleet weighs it in their reasoning.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {rows.map((r) => (
            <div
              key={r.id}
              className="grid grid-cols-[110px_140px_1fr_auto] gap-2 items-start"
            >
              <input
                type="text"
                placeholder="TICKER"
                value={r.ticker}
                onChange={(e) =>
                  updateRow(r.id, { ticker: e.target.value.toUpperCase() })
                }
                className="bg-background border border-border rounded-md px-3 py-1.5 text-sm font-mono uppercase"
                maxLength={8}
              />
              <select
                value={r.action}
                onChange={(e) =>
                  updateRow(r.id, { action: e.target.value as ActionHint })
                }
                className="bg-background border border-border rounded-md px-2 py-1.5 text-sm"
              >
                {ACTIONS.map((a) => (
                  <option key={a.value} value={a.value}>
                    {a.label}
                  </option>
                ))}
              </select>
              <textarea
                placeholder="Why? (this rationale is given to the fleet as your stated reasoning)"
                value={r.rationale}
                onChange={(e) => updateRow(r.id, { rationale: e.target.value })}
                className="bg-background border border-border rounded-md px-3 py-1.5 text-sm min-h-[40px]"
                rows={2}
              />
              <Button
                type="button"
                variant="outline"
                size="icon"
                onClick={() => removeRow(r.id)}
                disabled={rows.length === 1}
                title="Remove this row"
              >
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          ))}

          <div className="flex items-center justify-between">
            <Button type="button" variant="outline" onClick={addRow} size="sm">
              <Plus className="h-4 w-4 mr-1" /> Add ticker
            </Button>

            <div className="flex items-center gap-3 flex-wrap">
              <label className="text-xs text-muted-foreground flex items-center gap-1">
                mode
                <span
                  className="inline-flex"
                  title={
                    "Mode = how the fleet weighs the inputs.\n\n" +
                    "Long hold (default): 4 analysts — fundamentals, news, sentiment, macro. Trader weighs thesis fit + dividend record + multi-year horizon. Skips technical chart timing (MACD/RSI/MA crossings) and per-ticker FX hedging — they don't apply to a 5+ year holding decision.\n\n" +
                    "Tactical trade: 6 analysts — adds technical (MACD/RSI/MA) + FX. Trader timing-focused (entry/exit, limits, time-in-force). Right call when you're sizing an entry, not deciding whether to own the company long term."
                  }
                >
                  <HelpCircle className="h-3 w-3 text-muted-foreground" />
                </span>
              </label>
              <select
                value={mode}
                onChange={(e) => setMode(e.target.value as ConsultMode)}
                className="bg-background border border-border rounded-md px-2 py-1 text-xs"
              >
                {MODES.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>

              <label className="text-xs text-muted-foreground flex items-center gap-1">
                tier
                <span
                  className="inline-flex"
                  title={
                    "Tier = how many checks the fleet runs before the decision can execute.\n\n" +
                    "T1: small live trades. Researcher + trader + light risk review. Single human approval.\n" +
                    "T2: medium trades. Full bull/bear debate + risk team + fund manager. The default for most decisions.\n" +
                    "T3: large or plan-structural changes. Adds a second-factor approval (TOTP or delay) on top of T2."
                  }
                >
                  <HelpCircle className="h-3 w-3 text-muted-foreground" />
                </span>
              </label>
              <select
                value={tier}
                onChange={(e) => setTier(e.target.value as (typeof TIERS)[number])}
                className="bg-background border border-border rounded-md px-2 py-1 text-xs font-mono"
              >
                {TIERS.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
              <Button onClick={onSubmit} disabled={!canSubmit}>
                {submitting
                  ? `Running ${rows.length} consult${rows.length === 1 ? "" : "s"}…`
                  : `Run consult (${rows.length})`}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      {outcomes.length > 0 && (
        <section className="flex flex-col gap-3">
          <h2 className="text-sm font-semibold tracking-wide uppercase text-muted-foreground">
            Results
          </h2>
          {outcomes.map((o, i) => (
            <Card key={`${o.ticker}-${i}`}>
              <CardHeader>
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <CardTitle className="text-base flex items-center gap-2">
                      {o.ticker}
                      <Badge variant={actionVariant(o.action)}>
                        {o.action.replace("_", " ").toUpperCase()}
                      </Badge>
                      {o.state === "running" && (
                        <Badge variant="secondary">running…</Badge>
                      )}
                      {o.state === "ok" && o.result && (() => {
                        const v = verdictFor(o.action, o.result);
                        return (
                          <Badge
                            variant={
                              v.tone === "warning"
                                ? "warning"
                                : v.tone === "success"
                                  ? "success"
                                  : v.tone === "error"
                                    ? "error"
                                    : "secondary"
                            }
                          >
                            {v.verdict} · {o.result.tier}
                          </Badge>
                        );
                      })()}
                      {o.state === "error" && (
                        <Badge variant="error">failed</Badge>
                      )}
                    </CardTitle>
                    <CardDescription className="text-xs">
                      Your rationale: {o.rationale || "(none provided)"}
                    </CardDescription>
                  </div>
                  {o.state === "ok" && o.result && (
                    <div className="text-right text-xs font-mono text-muted-foreground">
                      <div>run #{o.result.decision_run_id}</div>
                      {o.result.proposal_id != null && (
                        <Link
                          href={`/proposals`}
                          className="text-primary hover:underline"
                        >
                          → Proposal #{o.result.proposal_id}
                        </Link>
                      )}
                    </div>
                  )}
                </div>
              </CardHeader>
              {o.state === "ok" && o.result && (() => {
                const parsed = parseOutcome(o.action, o.result);
                const leanLabel = o.action.replace("_", " ").toUpperCase();
                return (
                  <CardContent className="flex flex-col gap-4">
                    {/* Verdict heading */}
                    <div className="flex items-baseline gap-3 flex-wrap">
                      <span
                        className={`text-3xl font-bold tracking-tight ${verdictHeadingClass(parsed.verdictTone)}`}
                      >
                        {parsed.verdict}
                      </span>
                      {parsed.blockedByLabel && parsed.verdict !== "HOLD" && (
                        <span className="text-xs text-muted-foreground">
                          blocked by {parsed.blockedByLabel.toLowerCase()}
                        </span>
                      )}
                      <span className="text-xs text-muted-foreground ml-auto">
                        Your lean: <span className="font-semibold">{leanLabel}</span>{" "}
                        · Fleet verdict:{" "}
                        <span className="font-semibold">{parsed.verdict}</span>
                      </span>
                    </div>

                    {/* One-line summary */}
                    {parsed.summary && (
                      <p className="text-sm font-medium leading-relaxed">
                        {parsed.summary}
                      </p>
                    )}

                    {/* Reasoning (rest of the body, citations stripped) */}
                    {parsed.body && (
                      <div className="text-sm text-muted-foreground leading-relaxed whitespace-pre-wrap">
                        {parsed.body}
                      </div>
                    )}

                    {/* What was considered — citation pills */}
                    {parsed.citations.length > 0 && (
                      <div className="flex flex-col gap-1.5">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                          What was considered
                        </span>
                        <div className="flex flex-wrap gap-1.5">
                          {parsed.citations.map((c) => (
                            <Badge
                              key={c}
                              variant="secondary"
                              className="font-mono text-[11px]"
                            >
                              {c}
                            </Badge>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Fall-back: if parsing left us with nothing (no summary,
                        no body, no citations) but the backend did return a
                        blocked_reason, show it raw so we don't hide signal. */}
                    {!parsed.summary &&
                      !parsed.body &&
                      parsed.citations.length === 0 &&
                      o.result.blocked_reason && (
                        <p className="text-sm text-muted-foreground">
                          {o.result.blocked_reason}
                        </p>
                      )}

                    {/* Deep-link to the full run */}
                    <div className="pt-1">
                      <Link
                        href={`/decisions/${o.result.decision_run_id}`}
                        className="text-sm text-primary hover:underline"
                      >
                        View full run →
                      </Link>
                    </div>
                  </CardContent>
                );
              })()}
              {o.state === "error" && (
                <CardContent>
                  <p className="text-sm font-mono text-error">{o.error}</p>
                </CardContent>
              )}
              {o.state === "running" && (
                <CardContent>
                  <p className="text-sm text-muted-foreground">
                    The fleet is running — analysts → debate → trader → risk → fund manager.
                    A single ticker typically takes 3–10 minutes.
                  </p>
                </CardContent>
              )}
            </Card>
          ))}
        </section>
      )}
    </div>
  );
}

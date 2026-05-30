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

export default function ConsultPage() {
  const [rows, setRows] = useState<InputRow[]>([newRow()]);
  const [tier, setTier] = useState<(typeof TIERS)[number]>("T2");
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
    <main className="max-w-5xl mx-auto p-6 flex flex-col gap-6">
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

            <div className="flex items-center gap-3">
              <label className="text-xs text-muted-foreground flex items-center gap-1">
                tier
                <span
                  className="inline-flex"
                  title={
                    "Tier = how many checks the fleet runs before the decision can execute.\n\n" +
                    "auto: resolver picks based on proposed size + ticker + account class.\n" +
                    "T0:  paper-only sandbox (researcher + trader, no fund manager). Fastest, no real-money path.\n" +
                    "T1:  small live trades. Researcher + trader + light risk review. Single human approval.\n" +
                    "T2:  medium trades. Full bull/bear debate + risk team + fund manager. The default for most decisions.\n" +
                    "T3:  large or plan-structural changes. Adds a second-factor approval (TOTP or delay) on top of T2."
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
                      {o.state === "ok" && o.result && (
                        <Badge
                          variant={
                            o.result.status === "approved" ? "success" : "error"
                          }
                        >
                          {o.result.status} · {o.result.tier}
                        </Badge>
                      )}
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
              {o.state === "ok" && o.result && (
                <CardContent>
                  {o.result.status === "blocked" ? (
                    <p className="text-sm">
                      <span className="font-semibold">Blocked by</span>{" "}
                      <span className="font-mono">{o.result.blocked_by}</span>:{" "}
                      {o.result.blocked_reason}
                    </p>
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      Approved — see{" "}
                      <Link
                        href={`/decisions/${o.result.decision_run_id}`}
                        className="text-primary hover:underline"
                      >
                        full agent cascade and reasoning trail
                      </Link>{" "}
                      for details.
                    </p>
                  )}
                </CardContent>
              )}
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
    </main>
  );
}

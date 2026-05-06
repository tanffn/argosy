"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

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
  type ArgonautSnapshot,
  type ArgonautStatus,
  type ArgonautTrade,
  type DraftResponse,
} from "@/lib/api";

const USER_ID = "ariel";

type Mode = "paper" | "live" | "queue_only";

function modeBadgeVariant(mode: string) {
  if (mode === "live") return "default" as const;
  if (mode === "queue_only") return "secondary" as const;
  return "outline" as const;
}

function fmtUsd(v: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(v);
}

interface SimpleSparkProps {
  rows: ArgonautSnapshot[];
}

function PnLSpark({ rows }: SimpleSparkProps) {
  if (!rows.length) {
    return (
      <p className="text-sm text-muted-foreground">
        No snapshots yet. Use Force snapshot to record one.
      </p>
    );
  }
  const min = Math.min(...rows.map((r) => r.total_value_usd));
  const max = Math.max(...rows.map((r) => r.total_value_usd));
  const range = max - min || 1;
  const W = 600;
  const H = 120;
  const points = rows.map((r, i) => {
    const x = (i / Math.max(1, rows.length - 1)) * W;
    const y = H - ((r.total_value_usd - min) / range) * H;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg
      width="100%"
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      className="border border-border rounded-md"
    >
      <polyline
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        points={points.join(" ")}
      />
    </svg>
  );
}

export default function ArgonautPage() {
  const [status, setStatus] = useState<ArgonautStatus | null>(null);
  const [snapshots, setSnapshots] = useState<ArgonautSnapshot[]>([]);
  const [trades, setTrades] = useState<ArgonautTrade[]>([]);
  const [pending, setPending] = useState<Mode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [planCurrent, setPlanCurrent] = useState<DraftResponse | null>(null);
  const [takingTicker, setTakingTicker] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, snaps, t] = await Promise.all([
        api.argonautStatus(USER_ID),
        api.argonautSnapshots(USER_ID),
        api.argonautTrades(USER_ID),
      ]);
      setStatus(s);
      setSnapshots(snaps.rows);
      setTrades(t.rows);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Pull the user's accepted plan once. 404 (no current plan yet) is
  // expected for fresh installs — silently set null so the panel hides.
  useEffect(() => {
    api
      .planCurrentStructured(USER_ID)
      .then(setPlanCurrent)
      .catch(() => setPlanCurrent(null));
  }, []);

  const onTake = useCallback(
    async (ticker: string) => {
      setError(null);
      setTakingTicker(ticker);
      try {
        await api.planSpeculativeTake(USER_ID, ticker, "paper");
        // Surface the routed proposal in the trades/positions panels.
        await refresh();
        // TODO(toast-migration): the project has no toast infrastructure
        // yet (no sonner / no `useToast`); when one lands (likely shadcn
        // ``toast``), replace these blocking ``window.alert`` calls with
        // a non-blocking success toast and an error toast respectively.
        window.alert(`Routed ${ticker} to Argonaut paper queue`);
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        // TODO(toast-migration): see above — replace with an error toast
        // once toast infra exists.
        window.alert(msg);
      } finally {
        setTakingTicker(null);
      }
    },
    [refresh],
  );

  const handleMode = useCallback(
    async (mode: Mode) => {
      const ok = window.confirm(
        `Switch Argonaut mode to "${mode}"? This rewrites agent_settings.yaml.`,
      );
      if (!ok) return;
      setPending(mode);
      try {
        await api.argonautMode(USER_ID, mode);
        await refresh();
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setPending(null);
      }
    },
    [refresh],
  );

  const handleForceSnapshot = useCallback(async () => {
    try {
      await api.argonautForceSnapshot(USER_ID);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }, [refresh]);

  const totalDayPnl = useMemo(
    () => (snapshots.length ? snapshots[snapshots.length - 1].day_pnl_usd : 0),
    [snapshots],
  );

  return (
    <main className="max-w-6xl mx-auto px-6 py-6 space-y-6">
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Argonaut</h1>
          <p className="text-sm text-muted-foreground">
            Limited-account autonomous trading. T0/T1 auto-executes; T2/T3
            still requires human approval.
          </p>
        </div>
        <div className="flex gap-2 items-center">
          <Button variant="outline" onClick={() => void handleForceSnapshot()}>
            Force snapshot
          </Button>
          <Button variant="outline" onClick={() => void refresh()}>
            Refresh
          </Button>
        </div>
      </div>

      {error && (
        <Card>
          <CardHeader>
            <CardTitle className="text-destructive">Error</CardTitle>
          </CardHeader>
          <CardContent>{error}</CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Account</CardTitle>
            <CardDescription>
              {status ? status.account_id : "loading..."}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div>Configured size: {status ? fmtUsd(status.size_usd) : "-"}</div>
            <div>
              Mode:{" "}
              {status && (
                <Badge variant={modeBadgeVariant(status.execution_mode)}>
                  {status.execution_mode}
                </Badge>
              )}{" "}
              Autonomy:{" "}
              {status?.autonomy_enabled ? (
                <Badge variant="success">enabled</Badge>
              ) : (
                <Badge variant="secondary">disabled</Badge>
              )}
            </div>
            <div>
              Per-decision max: {status?.per_decision_max_pct ?? "-"}% — Daily
              loss limit: {status?.daily_loss_limit_pct ?? "-"}%
            </div>
            <div className="flex gap-2 pt-2">
              {(["paper", "live", "queue_only"] as Mode[]).map((m) => (
                <Button
                  key={m}
                  size="sm"
                  variant={status?.execution_mode === m ? "default" : "outline"}
                  disabled={pending !== null || status?.execution_mode === m}
                  onClick={() => void handleMode(m)}
                >
                  {m}
                </Button>
              ))}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>P&amp;L (since inception)</CardTitle>
            <CardDescription>
              {snapshots.length} snapshot(s); last day Δ{" "}
              {totalDayPnl >= 0 ? "+" : ""}
              {fmtUsd(totalDayPnl)}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <PnLSpark rows={snapshots} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Open positions</CardTitle>
        </CardHeader>
        <CardContent>
          {!status || status.open_positions.length === 0 ? (
            <p className="text-sm text-muted-foreground">No open positions.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="py-1">Ticker</th>
                  <th className="py-1">Qty</th>
                  <th className="py-1">Avg cost</th>
                  <th className="py-1">Currency</th>
                </tr>
              </thead>
              <tbody>
                {status.open_positions.map((p) => (
                  <tr key={p.ticker} className="border-t border-border">
                    <td className="py-1">{p.ticker}</td>
                    <td className="py-1">{p.quantity}</td>
                    <td className="py-1">
                      {p.avg_cost !== null ? fmtUsd(p.avg_cost) : "-"}
                    </td>
                    <td className="py-1">{p.currency}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      {planCurrent?.horizon_short &&
      planCurrent.horizon_short.speculative_candidates.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Speculative candidates this month
            </CardTitle>
            <CardDescription>
              Bounded-risk shots surfaced by the fleet. Each is within your
              speculation cap.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="flex flex-col gap-2">
              {planCurrent.horizon_short.speculative_candidates.map((c, i) => (
                <li
                  key={i}
                  className="border border-border rounded-md p-3 flex items-start justify-between gap-3"
                >
                  <div className="text-sm">
                    <strong>{c.ticker}</strong> — {c.thesis_summary}
                    <br />
                    <span className="text-xs text-muted-foreground">
                      ≤ ${c.suggested_position_usd.toLocaleString()} · exit:{" "}
                      {c.exit_trigger}
                    </span>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={takingTicker !== null}
                      onClick={() => void onTake(c.ticker)}
                    >
                      {takingTicker === c.ticker
                        ? "Routing..."
                        : "Take a swing"}
                    </Button>
                    <Button size="sm" variant="ghost" disabled>
                      Skip
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Recent trades</CardTitle>
          <CardDescription>Paper + live fills (most recent first)</CardDescription>
        </CardHeader>
        <CardContent>
          {trades.length === 0 ? (
            <p className="text-sm text-muted-foreground">No trades yet.</p>
          ) : (
            <table className="w-full text-sm">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="py-1">When</th>
                  <th className="py-1">Mode</th>
                  <th className="py-1">Ticker</th>
                  <th className="py-1">Action</th>
                  <th className="py-1">Qty</th>
                  <th className="py-1">Price</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t) => (
                  <tr key={t.id} className="border-t border-border">
                    <td className="py-1">
                      {new Date(t.filled_at).toLocaleString()}
                    </td>
                    <td className="py-1">
                      <Badge variant={t.paper ? "outline" : "default"}>
                        {t.paper ? "paper" : "live"}
                      </Badge>
                    </td>
                    <td className="py-1">{t.ticker}</td>
                    <td className="py-1">{t.action}</td>
                    <td className="py-1">{t.quantity}</td>
                    <td className="py-1">{fmtUsd(t.price)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Per-strategy stats</CardTitle>
          <CardDescription>
            Win rate, avg holding period — populated in Phase 6+.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Coming soon: aggregate stats per strategy / ticker bucket.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Deposit / withdraw</CardTitle>
          <CardDescription>
            Capital movements happen via IBKR directly; configure size in
            <code className="mx-1">agent_settings.yaml</code>.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            Phase 5 placeholder — actual deposit/withdraw happens at the
            broker. The configured size (
            <strong>{status ? fmtUsd(status.size_usd) : "-"}</strong>) is the
            denominator for the tier-escalation rule.
          </p>
        </CardContent>
      </Card>
    </main>
  );
}

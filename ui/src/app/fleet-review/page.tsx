"use client";

/**
 * Fleet self-review LIST view + 30-day severity-trends chart.
 *
 * Today's surfaces:
 *   /fleet-review        — this page (chart + persistent-findings + recent rows)
 *   /fleet-review/{id}   — full-report viewer (existing)
 *
 * Before this page existed the only way to reach a report was via a
 * direct URL or the home-page banner — the user had no read surface
 * for "is the AMBER count going up?".  The chart on top of this page
 * is that surface.  The bulleted "most persistent findings" call-out
 * names the SPECIFIC detectors that keep tripping across runs so the
 * fix priority is obvious without opening each report.
 *
 * Chart choice: stacked area.  RED is layered on top of AMBER on top
 * of YELLOW so a small RED spike is immediately visible without being
 * hidden inside a tall YELLOW band.  Recharts is the project's chart
 * library — same primitive used in plan/allocation-chart.tsx.
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type FleetSelfReviewListItemDTO,
  type FleetSelfReviewTrendsDTO,
} from "@/lib/api";

const USER_ID = "ariel";
const TREND_DAYS = 30;
const LIST_LIMIT = 50;

// Severity palette — tuned to read distinctly when stacked. RED is
// vivid so a single hit doesn't get lost above a tall YELLOW band.
const SEV_COLORS = {
  red: "#ef4444",
  amber: "#f59e0b",
  yellow: "#eab308",
};

interface TrendChartRow {
  /** Short label rendered on the X-axis tick. */
  label: string;
  /** ISO string used for the tooltip header. */
  iso: string;
  red: number;
  amber: number;
  yellow: number;
}

function buildChartRows(trends: FleetSelfReviewTrendsDTO): TrendChartRow[] {
  return trends.points.map((p) => {
    const d = new Date(p.generated_at);
    // "MM-DD HH:mm" is enough resolution: post_synthesis reports cluster
    // around plan revisions, so the time-of-day matters for ordering.
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    return {
      label: `${mm}-${dd} ${hh}:${mi}`,
      iso: p.generated_at,
      red: p.red,
      amber: p.amber,
      yellow: p.yellow,
    };
  });
}

export default function FleetReviewListPage() {
  const [trends, setTrends] = useState<FleetSelfReviewTrendsDTO | null>(null);
  const [reports, setReports] = useState<FleetSelfReviewListItemDTO[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setLoading(true);
        const [t, r] = await Promise.all([
          api.fleetSelfReviewTrends(USER_ID, TREND_DAYS),
          api.fleetSelfReviewList(USER_ID, LIST_LIMIT),
        ]);
        if (!cancelled) {
          setTrends(t);
          setReports(r);
        }
      } catch (e: unknown) {
        if (!cancelled) setError(String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const chartRows = useMemo<TrendChartRow[]>(
    () => (trends ? buildChartRows(trends) : []),
    [trends],
  );

  if (loading) {
    return (
      <main className="max-w-6xl mx-auto p-6">
        <p className="text-sm font-mono text-muted-foreground">Loading...</p>
      </main>
    );
  }
  if (error) {
    return (
      <main className="max-w-6xl mx-auto p-6">
        <p className="text-sm font-mono text-error">Error: {error}</p>
      </main>
    );
  }

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="font-mono text-xl font-semibold">
          Fleet self-review
        </h1>
        <p className="text-xs font-mono text-muted-foreground mt-1">
          {TREND_DAYS}-day severity trends &amp; recent reports.
        </p>
      </header>

      <section>
        <Card>
          <CardHeader>
            <CardTitle className="font-mono">
              Severity trends ({TREND_DAYS} days,{" "}
              {trends?.report_count ?? 0} reports)
            </CardTitle>
            <CardDescription>
              Stacked counts of RED / AMBER / YELLOW findings per
              report.  One point = one persisted sweep.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {chartRows.length === 0 ? (
              <p className="text-sm text-muted-foreground py-8 text-center">
                No fleet self-review reports in the last {TREND_DAYS} days.
              </p>
            ) : (
              <ResponsiveContainer width="100%" height={260}>
                <AreaChart
                  data={chartRows}
                  margin={{ top: 8, right: 24, bottom: 8, left: 4 }}
                >
                  <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                  <XAxis dataKey="label" fontSize={10} />
                  <YAxis allowDecimals={false} fontSize={10} />
                  <Tooltip
                    labelFormatter={(label, payload) => {
                      const arr = Array.isArray(payload) ? payload : [];
                      const first = arr[0] as
                        | { payload?: { iso?: string } }
                        | undefined;
                      const iso = first?.payload?.iso;
                      return iso ? new Date(iso).toLocaleString() : label;
                    }}
                  />
                  <Legend />
                  {/* Stacked bottom-to-top: YELLOW -> AMBER -> RED so
                      a single RED hit sits at the top of the stack and
                      is always visible. */}
                  <Area
                    type="monotone"
                    dataKey="yellow"
                    stackId="sev"
                    stroke={SEV_COLORS.yellow}
                    fill={SEV_COLORS.yellow}
                    fillOpacity={0.5}
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="amber"
                    stackId="sev"
                    stroke={SEV_COLORS.amber}
                    fill={SEV_COLORS.amber}
                    fillOpacity={0.55}
                    isAnimationActive={false}
                  />
                  <Area
                    type="monotone"
                    dataKey="red"
                    stackId="sev"
                    stroke={SEV_COLORS.red}
                    fill={SEV_COLORS.red}
                    fillOpacity={0.7}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            )}

            {trends && trends.most_persistent_findings.length > 0 ? (
              <div className="mt-4 border-t border-border/40 pt-3">
                <div className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground mb-2">
                  Most persistent findings (in &ge;50% of runs)
                </div>
                <ul className="flex flex-col gap-1 text-xs font-mono">
                  {trends.most_persistent_findings.map((label) => (
                    <li key={label} className="flex items-start gap-2">
                      <span className="text-muted-foreground">&bull;</span>
                      <span>{label}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : trends && trends.report_count >= 2 ? (
              <p className="mt-4 text-xs font-mono text-muted-foreground">
                No finding present in &ge;50% of the last{" "}
                {trends.report_count} runs.
              </p>
            ) : null}
          </CardContent>
        </Card>
      </section>

      <section>
        <Card>
          <CardHeader>
            <CardTitle className="font-mono">
              Recent reports ({reports.length})
            </CardTitle>
            <CardDescription>
              Newest first.  Click a row to open the full report.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {reports.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4">
                No reports yet.
              </p>
            ) : (
              <ul className="flex flex-col divide-y divide-border/60">
                {reports.map((r) => (
                  <ReportRow key={r.id} report={r} />
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </section>
    </main>
  );
}

function ReportRow({ report }: { report: FleetSelfReviewListItemDTO }) {
  const sev = report.severity_summary;
  const red = sev.RED ?? 0;
  const amber = sev.AMBER ?? 0;
  const yellow = sev.YELLOW ?? 0;
  return (
    <li>
      <Link
        href={`/fleet-review/${report.id}`}
        className="flex items-center gap-3 py-2 px-1 hover:bg-secondary/30 rounded text-sm font-mono"
      >
        <span className="text-muted-foreground w-10 shrink-0">
          #{report.id}
        </span>
        <span className="text-muted-foreground w-44 shrink-0">
          {new Date(report.generated_at).toLocaleString()}
        </span>
        <span className="text-muted-foreground w-32 shrink-0">
          {report.scope_kind}
        </span>
        <span className="text-muted-foreground w-24 shrink-0">
          {report.decision_run_id !== null
            ? `run #${report.decision_run_id}`
            : "(no run)"}
        </span>
        <span className="flex items-center gap-1.5 flex-1">
          <StatusPill tone="error" mono>
            RED {red}
          </StatusPill>
          <StatusPill tone="warning" mono>
            AMBER {amber}
          </StatusPill>
          <StatusPill tone="neutral" mono>
            YELLOW {yellow}
          </StatusPill>
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground ml-2">
            {report.findings_total} findings
          </span>
        </span>
        <span className="text-info">&rarr;</span>
      </Link>
    </li>
  );
}

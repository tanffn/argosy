"use client";

// Decisions list (index) page — surfaces the recent decision_runs as a
// clickable table that lands on /decisions/[id] for the full agent tree
// + cost breakdown. The detail page already exists; this page is the
// missing entry point.
//
// Data source: GET /api/decisions/recent (argosy/api/routes/decisions.py).
// User filter is fixed to "ariel" (single-user system today; multi-tenant
// ready by design but no UI selector is needed yet — sibling pages like
// /audit and /agents do the same).

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select";
import { StatusPill } from "@/components/ui/status-pill";
import { api, type DecisionGroup } from "@/lib/api";

const USER_ID = "ariel";

// ---------------------------------------------------------------------------
// Filter constants — kept in module scope so the Select component
// receives a stable identity across renders.
// ---------------------------------------------------------------------------

const KIND_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "All kinds" },
  { value: "trade_proposal", label: "Trade proposal" },
  { value: "plan_revision", label: "Plan revision" },
  { value: "plan_amendment_chat", label: "Plan amendment chat" },
  { value: "delta_pushback", label: "Delta pushback" },
  { value: "daily_brief", label: "Daily brief" },
];

const LIMIT_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "20", label: "20 rows" },
  { value: "50", label: "50 rows" },
  { value: "100", label: "100 rows" },
];

// ---------------------------------------------------------------------------
// Formatting helpers — kept inline (one file) per the brief; mirror the
// shape used on the detail page (parseAsUTC) so the same naive-ISO row
// renders identically in both places.
// ---------------------------------------------------------------------------

// Backend serializes decision_runs.started_at via plain .isoformat(),
// which drops tzinfo for the naive datetimes SQLite stores. The JS Date
// constructor then interprets a tz-less ISO string as LOCAL time, so we
// pin it to UTC the same way the detail page does.
function parseAsUTC(iso: string): number {
  const hasTz = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso);
  return Date.parse(hasTz ? iso : iso + "Z");
}

// "MMM DD HH:mm" — short, sortable enough to scan, never wider than 12ch.
function formatStarted(iso: string): string {
  const ms = parseAsUTC(iso);
  if (Number.isNaN(ms)) return iso;
  const d = new Date(ms);
  const months = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];
  const month = months[d.getUTCMonth()];
  const day = String(d.getUTCDate()).padStart(2, "0");
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${month} ${day} ${hh}:${mm}`;
}

// "4m 12s" / "1h 3m" — humanish; falls back to "running" if the run has
// no finished_at (matches the status-column language).
function formatDuration(startedAt: string, finishedAt: string | null): string {
  if (!finishedAt) return "running";
  const start = parseAsUTC(startedAt);
  const end = parseAsUTC(finishedAt);
  if (Number.isNaN(start) || Number.isNaN(end)) return "—";
  const diffS = Math.max(0, Math.round((end - start) / 1000));
  if (diffS < 60) return `${diffS}s`;
  const m = Math.floor(diffS / 60);
  const s = diffS % 60;
  if (m < 60) return s === 0 ? `${m}m` : `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return remM === 0 ? `${h}h` : `${h}h ${remM}m`;
}

// Humanized labels for the decision_kind taxonomy. Mirrors the backend
// _KIND_LABELS map. Unlisted kinds fall back to a Title-cased version of
// the raw value so a newly-added kind never renders blank.
const KIND_LABELS: Record<string, string> = {
  trade_proposal: "Trade proposal",
  plan_revision: "Plan synthesis / revision",
  plan_amendment_chat: "Plan amendment chat",
  delta_pushback: "Delta pushback",
  daily_brief: "Daily brief",
};

function humanizeKind(kind: string | null): string {
  if (!kind) return "—";
  return (
    KIND_LABELS[kind] ??
    kind
      .replace(/_/g, " ")
      .replace(/^./, (c) => c.toUpperCase())
  );
}

// Short human description for the row. Prefers the backend-derived
// `description` (built from real DecisionRun fields). Falls back to the
// most recent agent_run.response_text when present (trade runs), then to
// "—" for brand-new "running" rows with nothing factual yet.
function describeRun(g: DecisionGroup): string {
  if (g.description && g.description.trim().length > 0) {
    return g.description.trim();
  }
  for (let i = g.agent_runs.length - 1; i >= 0; i -= 1) {
    const t = g.agent_runs[i]?.response_text;
    if (t && t.trim().length > 0) {
      const oneLine = t.replace(/\s+/g, " ").trim();
      return oneLine.length > 120 ? `${oneLine.slice(0, 117)}…` : oneLine;
    }
  }
  return "—";
}

// Map a backend status string to the StatusPill tone palette. Unknown
// values fall through to "neutral" so a new backend status doesn't break
// the UI — it just renders without a color.
function statusTone(
  status: string,
): "success" | "warning" | "error" | "neutral" | "accent" {
  switch (status) {
    case "approved":
      return "success";
    case "blocked":
    case "hold":
      return "warning";
    case "failed":
      return "error";
    case "running":
      return "accent";
    case "completed":
    default:
      return "neutral";
  }
}

// ---------------------------------------------------------------------------
// Page component
// ---------------------------------------------------------------------------

export default function DecisionsListPage() {
  const [rows, setRows] = useState<DecisionGroup[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [decisionKind, setDecisionKind] = useState<string>("");
  const [limit, setLimit] = useState<string>("20");

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const data = await api.decisionsRecent(USER_ID, Number(limit), {
        decisionKind: decisionKind || undefined,
      });
      setRows(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setRows(null);
    } finally {
      setLoading(false);
    }
  }, [decisionKind, limit]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount; refresh() sets local state from the API
    refresh();
  }, [refresh]);

  const skeletonRows = useMemo(
    () => Array.from({ length: 6 }, (_, i) => i),
    [],
  );

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h1 className="text-2xl font-semibold tracking-tight">Decisions</h1>
          <Link
            href="/decisions/funnel"
            className="text-sm text-primary hover:underline font-medium"
          >
            Decision funnel runs (debug) →
          </Link>
        </div>
        <p className="text-sm text-muted-foreground">
          Recent decision_runs from the agent fleet — synthesis runs, plan
          revisions, daily briefs, delta pushbacks. Click a row for the full
          agent tree, cost breakdown, and transcripts.
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Filters</CardTitle>
          <CardDescription>
            Ordered most-recent first. Kind filter is applied server-side.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-2 md:grid-cols-4 gap-3 items-end">
          <label className="flex flex-col text-xs gap-1">
            <span className="text-muted-foreground">Kind</span>
            <Select value={decisionKind} onValueChange={setDecisionKind}>
              <SelectTrigger />
              <SelectContent>
                {KIND_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
          <label className="flex flex-col text-xs gap-1">
            <span className="text-muted-foreground">Limit</span>
            <Select value={limit} onValueChange={setLimit}>
              <SelectTrigger />
              <SelectContent>
                {LIMIT_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </label>
        </CardContent>
      </Card>

      {error && (
        <Card>
          <CardContent className="py-6 text-sm text-error font-mono">
            Couldn&apos;t load decision history — {error}
          </CardContent>
        </Card>
      )}

      {loading && !error && (
        <Card>
          <CardContent className="p-0">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-[10px] uppercase tracking-wide text-muted-foreground border-b border-border">
                  <th className="text-left py-2 px-3">Ticker</th>
                  <th className="text-left py-2 px-3">Tier</th>
                  <th className="text-left py-2 px-3">Kind</th>
                  <th className="text-left py-2 px-3">Status</th>
                  <th className="text-left py-2 px-3">Started</th>
                  <th className="text-left py-2 px-3">Duration</th>
                  <th className="text-right py-2 px-3">Agents</th>
                  <th className="text-right py-2 px-3">Cost</th>
                  <th className="text-left py-2 px-3">Description</th>
                </tr>
              </thead>
              <tbody>
                {skeletonRows.map((i) => (
                  <tr key={i} className="border-b border-border/40">
                    <td colSpan={9} className="py-3 px-3">
                      <div className="h-4 w-full rounded bg-secondary/40 animate-pulse" />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      {!loading && !error && rows !== null && rows.length === 0 && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No decision runs yet.
          </CardContent>
        </Card>
      )}

      {!loading && !error && rows !== null && rows.length > 0 && (
        <Card>
          <CardContent className="p-0">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-[10px] uppercase tracking-wide text-muted-foreground border-b border-border">
                  <th className="text-left py-2 px-3 w-[80px]">Ticker</th>
                  <th className="text-left py-2 px-3 w-[60px]">Tier</th>
                  <th className="text-left py-2 px-3 w-[160px]">Kind</th>
                  <th className="text-left py-2 px-3 w-[100px]">Status</th>
                  <th className="text-left py-2 px-3 w-[110px]">Started</th>
                  <th className="text-left py-2 px-3 w-[90px]">Duration</th>
                  <th className="text-right py-2 px-3 w-[70px]">Agents</th>
                  <th className="text-right py-2 px-3 w-[80px]">Cost</th>
                  <th className="text-left py-2 px-3">Description</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((g) => (
                  <DecisionRow key={g.decision_id} group={g} />
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Row — co-located for the same reason JobsTable inlines its row: the
// column layout is tightly coupled to the parent <thead>.
// ---------------------------------------------------------------------------

function DecisionRow({ group }: { group: DecisionGroup }) {
  const tone = statusTone(group.status);
  const kind = humanizeKind(group.decision_kind);
  const description = describeRun(group);
  return (
    <tr className="border-b border-border/40 hover:bg-secondary/40 transition-colors">
      <td className="py-2 px-3 align-top">
        <Link
          href={`/decisions/${encodeURIComponent(group.decision_id)}`}
          className="font-mono font-medium text-foreground hover:underline"
        >
          {group.ticker ?? "—"}
        </Link>
      </td>
      <td className="py-2 px-3 align-top">
        {group.tier ? (
          <Badge variant="outline" className="font-mono text-[10px]">
            {group.tier}
          </Badge>
        ) : (
          <span className="text-muted-foreground text-xs">—</span>
        )}
      </td>
      <td className="py-2 px-3 align-top text-xs text-muted-foreground">
        {kind}
      </td>
      <td className="py-2 px-3 align-top">
        <StatusPill tone={tone} mono>
          {group.status}
        </StatusPill>
      </td>
      <td className="py-2 px-3 align-top text-xs font-mono text-muted-foreground tabular-nums whitespace-nowrap">
        {formatStarted(group.started_at)}
      </td>
      <td className="py-2 px-3 align-top text-xs font-mono text-muted-foreground tabular-nums whitespace-nowrap">
        {formatDuration(group.started_at, group.finished_at)}
      </td>
      <td className="py-2 px-3 align-top text-right tabular-nums">
        {group.agent_count}
      </td>
      <td className="py-2 px-3 align-top text-right tabular-nums font-mono text-xs">
        ${group.total_cost_usd.toFixed(2)}
      </td>
      <td className="py-2 px-3 align-top">
        <Link
          href={`/decisions/${encodeURIComponent(group.decision_id)}`}
          className="text-xs text-muted-foreground hover:text-foreground line-clamp-2"
          title={description}
        >
          {description}
        </Link>
      </td>
    </tr>
  );
}

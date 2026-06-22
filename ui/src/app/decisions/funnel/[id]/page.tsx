"use client";

// Decision-funnel run DETAIL (debug) — the full per-stage trace + immutable
// snapshots for one run. Answers "why did it (not) act on X?" without a re-run.
// DEBUG surface (under Decisions): internal fields are intentionally exposed.
// Data: GET /api/decisions/funnel/runs/{id}.

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type FunnelRunDetail,
  type FunnelSnapshotDTO,
  type FunnelStageRowDTO,
} from "@/lib/api";

const USER_ID = "ariel";
const STAGE_ORDER = ["stage0", "stage1", "stage2", "stage3", "surface"];
const STAGE_LABEL: Record<string, string> = {
  stage0: "Stage 0 — market review",
  stage1: "Stage 1 — relevance routing",
  stage2: "Stage 2 — triage",
  stage3: "Stage 3 — deep decision",
  surface: "Surface routing",
};

function decisionTone(d: string): "success" | "warning" | "error" | "neutral" | "accent" {
  if (["routed", "proposed", "surfaced", "triage_go"].includes(d)) return "success";
  if (["dropped", "hidden", "no_op", "triage_stop", "stage3_skipped", "deduped", "sleeve_deferred"].includes(d))
    return "neutral";
  if (["blocked", "error", "triage_error"].includes(d)) return "error";
  if (d === "risk_off") return "warning";
  return "accent";
}

function Json({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-muted-foreground">—</span>;
  return (
    <pre className="text-[11px] font-mono whitespace-pre-wrap break-all bg-muted/40 rounded p-2 overflow-x-auto">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function StageTable({ rows }: { rows: FunnelStageRowDTO[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-[10px] uppercase tracking-wide text-muted-foreground border-b border-border">
            <th className="text-left py-1.5 px-2">Subject</th>
            <th className="text-left py-1.5 px-2">Decision</th>
            <th className="text-left py-1.5 px-2">Signal / rule</th>
            <th className="text-left py-1.5 px-2">Reason</th>
            <th className="text-left py-1.5 px-2">Model</th>
            <th className="text-right py-1.5 px-2">Tokens</th>
            <th className="text-right py-1.5 px-2">Snap / Prop</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-b border-border/30 align-top">
              <td className="py-1.5 px-2 font-mono">
                {r.subject}
                <span className="ml-1 text-[10px] text-muted-foreground">{r.subject_type}</span>
              </td>
              <td className="py-1.5 px-2">
                <StatusPill tone={decisionTone(r.decision)} mono>
                  {r.decision}
                </StatusPill>
              </td>
              <td className="py-1.5 px-2 font-mono text-muted-foreground">{r.signal_or_rule ?? "—"}</td>
              <td className="py-1.5 px-2 text-muted-foreground max-w-[28ch]">{r.reason}</td>
              <td className="py-1.5 px-2 font-mono text-[10px] text-muted-foreground">{r.model ?? "—"}</td>
              <td className="py-1.5 px-2 text-right tabular-nums text-muted-foreground">
                {r.tokens_in != null || r.tokens_out != null
                  ? `${r.tokens_in ?? 0}/${r.tokens_out ?? 0}`
                  : "—"}
              </td>
              <td className="py-1.5 px-2 text-right font-mono text-[10px] text-muted-foreground">
                {r.snapshot_id ? `s${r.snapshot_id}` : "—"} {r.proposal_id ? `p${r.proposal_id}` : ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SnapshotItem({ s }: { s: FunnelSnapshotDTO }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-border/60 bg-background/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left hover:bg-secondary/40"
      >
        <span className="flex items-center gap-2 text-sm">
          <span aria-hidden className="text-muted-foreground">{open ? "▾" : "▸"}</span>
          <span className="font-mono font-semibold">{s.ticker}</span>
          <Badge variant="outline" className="text-[10px]">{s.human_action_state}</Badge>
          {s.why_not_act ? (
            <span className="text-xs text-muted-foreground">— {s.why_not_act}</span>
          ) : null}
        </span>
        <span className="text-[10px] font-mono text-muted-foreground">
          snap #{s.id} · {s.model_name ?? "?"}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 flex flex-col gap-2">
          <Field label="Decision (frozen)"><Json value={s.decision} /></Field>
          <Field label="Why not act">{s.why_not_act ?? "—"}</Field>
          <div className="grid md:grid-cols-2 gap-2">
            <Field label="Portfolio snapshot"><Json value={s.portfolio_snapshot} /></Field>
            <Field label="Market snapshot"><Json value={s.market_snapshot} /></Field>
          </div>
          <Field label="Policy"><Json value={s.policy} /></Field>
          <div className="grid md:grid-cols-2 gap-2 text-[11px] font-mono text-muted-foreground">
            <div>model: {s.model_name ?? "—"} {s.model_version ? `(${s.model_version})` : ""}</div>
            <div>prompt hash: {s.prompt_template_hash ?? "—"}</div>
            <div>policy ver: {s.policy_version ?? "—"}</div>
            <div>dedup: {s.dedup_key}</div>
            <div>proposal: {s.proposal_id ?? "—"}</div>
            <div>decision_run: {s.decision_run_id ?? "—"}</div>
          </div>
          {s.execution_drift ? <Field label="Execution drift"><Json value={s.execution_drift} /></Field> : null}
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground mb-0.5">{label}</div>
      <div className="text-sm">{children}</div>
    </div>
  );
}

export default function FunnelRunDetailPage(props: { params: Promise<{ id: string }> }) {
  const { id } = use(props.params);
  const [detail, setDetail] = useState<FunnelRunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const d = await api.funnelRunDetail(USER_ID, Number(id));
      setDetail(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount
    refresh();
  }, [refresh]);

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-5">
      <header>
        <Link href="/decisions/funnel" className="text-sm text-muted-foreground hover:underline">
          ← Funnel runs
        </Link>
        <h1 className="text-2xl font-semibold tracking-tight mt-1">
          Funnel run #{id}
        </h1>
      </header>

      {error && (
        <Card><CardContent className="py-6 text-sm text-error font-mono">{error}</CardContent></Card>
      )}
      {loading && !error && (
        <Card><CardContent className="py-10 text-center text-sm text-muted-foreground">Loading…</CardContent></Card>
      )}

      {detail && (
        <>
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                Run summary
                <StatusPill tone={detail.status === "ok" ? "success" : detail.status === "error" ? "error" : "neutral"} mono>
                  {detail.status}
                </StatusPill>
                <Badge variant={detail.shadow ? "outline" : "secondary"} className="text-[10px]">
                  {detail.shadow ? "shadow" : "live"}
                </Badge>
              </CardTitle>
            </CardHeader>
            <CardContent className="grid md:grid-cols-2 gap-2 text-[11px] font-mono text-muted-foreground">
              <div>trigger: {detail.trigger}</div>
              <div>started: {detail.started_at}</div>
              <div>finished: {detail.finished_at ?? "—"}</div>
              <div>policy: {detail.policy_version ?? "—"} · IPS: {detail.ips_version ?? "—"}</div>
              <div>plan version: {detail.plan_version_id ?? "—"}</div>
              {detail.error_message ? <div className="text-error">error: {detail.error_message}</div> : null}
              <div className="md:col-span-2 mt-1">
                <Field label="Totals"><Json value={detail.totals} /></Field>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader><CardTitle className="text-base">Stage 0 — market read</CardTitle></CardHeader>
            <CardContent><Json value={detail.macro_read} /></CardContent>
          </Card>

          {STAGE_ORDER.filter((st) => (detail.stages[st]?.length ?? 0) > 0).map((st) => (
            <Card key={st}>
              <CardHeader>
                <CardTitle className="text-base">
                  {STAGE_LABEL[st] ?? st}{" "}
                  <span className="text-xs font-normal text-muted-foreground">
                    ({detail.stages[st].length})
                  </span>
                </CardTitle>
              </CardHeader>
              <CardContent className="p-0 px-2 pb-2">
                <StageTable rows={detail.stages[st]} />
              </CardContent>
            </Card>
          ))}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                Immutable decision snapshots{" "}
                <span className="text-xs font-normal text-muted-foreground">
                  ({detail.snapshots.length})
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              {detail.snapshots.length === 0 ? (
                <p className="text-sm text-muted-foreground">No deep-decision snapshots in this run.</p>
              ) : (
                detail.snapshots.map((s) => <SnapshotItem key={s.id} s={s} />)
              )}
            </CardContent>
          </Card>
        </>
      )}
    </main>
  );
}

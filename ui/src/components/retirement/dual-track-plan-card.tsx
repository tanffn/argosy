"use client";

import { useEffect, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DrilldownSection } from "@/components/retirement/DrilldownSection";
import { MethodologyPanel } from "@/components/retirement/MethodologyPanel";
import { api, type DualTrackPlanResponse, type DualTrackTrack } from "@/lib/api";

interface Props {
  userId: string;
}

// ---- local formatting helpers ----------------------------------------
// (cards format their own NIS/pct locally — see ScenarioGridCard.)
const pct = (v: number | null | undefined): string =>
  v === null || v === undefined ? "—" : `${(v * 100).toFixed(0)}%`;

const age = (v: number | null | undefined): string =>
  v === null || v === undefined ? "—" : v.toFixed(0);

/** Compact NIS: ₪X.XM above a million, ₪Xk above a thousand, else ₪X. */
function nisCompact(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const abs = Math.abs(v);
  const sign = v < 0 ? "−" : "";
  if (abs >= 1_000_000) return `${sign}₪${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${sign}₪${Math.round(abs / 1_000)}k`;
  return `${sign}₪${Math.round(abs).toLocaleString()}`;
}

/** Full NIS with thousands separators — used in the assumptions panel. */
function nisFull(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `₪${Math.round(v).toLocaleString()}`;
}

/** Colour a solvency cell so the reader sees risk at a glance. */
function solventClass(v: number | null | undefined): string {
  if (v === null || v === undefined) return "text-muted-foreground";
  if (v >= 0.9) return "text-emerald-400";
  if (v >= 0.75) return "text-amber-400";
  return "text-rose-400";
}

const TRACK_ORDER: Record<DualTrackTrack["name"], number> = {
  bull: 0,
  typical: 1,
  bear: 2,
};

interface FrontierPoint {
  retire_age: number;
  median: number;
  worst10: number;
}

/** Read a numeric assumption, tolerating string-typed values in the bag. */
function numAssumption(
  a: Record<string, number | string>,
  key: string,
): number | undefined {
  const v = a[key];
  return typeof v === "number" ? v : undefined;
}

function strAssumption(
  a: Record<string, number | string>,
  key: string,
): string | undefined {
  const v = a[key];
  if (typeof v === "string") return v;
  if (typeof v === "number") return String(v);
  return undefined;
}

/**
 * DualTrackPlanCard — the retire-age ↔ estate-left-to-kids tradeoff.
 *
 * Two intents are surfaced side by side per return regime (bull / typical /
 * bear):
 *   - "Retire ASAP" (spend down) → the earliest age the spend-down plan still
 *     clears the 95-year solvency bar.
 *   - "Leave it to the kids" (preserve) → the earliest age the worst-10% path
 *     still preserves principal in real terms.
 *
 * Below the headline table is the estate frontier for the typical track
 * (median + worst-10% real estate by retire age), the spend-to-retire-now
 * lever, and a collapsible assumptions panel that makes every driving number
 * visible (output-trust doctrine).
 */
export function DualTrackPlanCard({ userId }: Props) {
  const [data, setData] = useState<DualTrackPlanResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .dualTrackPlan(userId)
      .then((d) => {
        if (!cancelled) {
          setErr(null);
          setData(d);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  if (err) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Retire now, or leave it to the kids?</CardTitle>
          <CardDescription className="text-rose-400">Failed: {err}</CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Retire now, or leave it to the kids?</CardTitle>
          <CardDescription>Running the dual-track Monte Carlo…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const tracks = [...data.tracks].sort(
    (a, b) => (TRACK_ORDER[a.name] ?? 99) - (TRACK_ORDER[b.name] ?? 99),
  );
  const typical = data.tracks.find((t) => t.name === "typical") ?? null;

  // Plain-English tradeoff line uses the typical track when present.
  const tDraw = typical?.drawdown_age ?? null;
  const tPres = typical?.preservation_age ?? null;
  const tradeoffYears =
    tDraw !== null && tPres !== null ? Math.max(0, Math.round(tPres - tDraw)) : null;

  // Estate frontier (typical track) → recharts rows in today's money.
  const frontier: FrontierPoint[] = (typical?.frontier ?? []).map((p) => ({
    retire_age: p.retire_age,
    median: p.median_estate_real_nis,
    worst10: p.worst10_estate_real_nis,
  }));

  const a = data.assumptions;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Retire now, or leave it to the kids?</CardTitle>
        <CardDescription>
          The retire-age ↔ estate tradeoff. Two intents — spend it down to retire
          ASAP, or preserve the principal for the kids — across three return
          regimes, on {nisCompact(data.deployable_nis)} deployable (today, age{" "}
          {age(data.current_age)}).
        </CardDescription>
      </CardHeader>
      <CardContent>
        {/* ---- Headline tradeoff table ---- */}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[10px] uppercase tracking-wider text-muted-foreground">
                <th className="text-left font-medium py-1.5">Return regime</th>
                <th className="text-right font-medium">
                  Retire ASAP
                  <div className="font-normal normal-case text-[9px] text-muted-foreground/70">
                    spend-down, 90% safe to 95
                  </div>
                </th>
                <th className="text-right font-medium">
                  Leave it to the kids
                  <div className="font-normal normal-case text-[9px] text-muted-foreground/70">
                    worst-10% preserves principal
                  </div>
                </th>
              </tr>
            </thead>
            <tbody className="tabular-nums">
              {tracks.map((t) => (
                <tr
                  key={t.name}
                  className={`border-t border-border/40 ${t.name === "typical" ? "bg-sky-500/10" : ""}`}
                >
                  <td className="py-2 text-foreground">
                    <span className="font-medium">{t.label}</span>
                    <span className="ml-1.5 font-mono text-[11px] text-muted-foreground">
                      {pct(t.mu_real)} real
                    </span>
                  </td>
                  <td className="text-right">
                    <div className="font-mono text-lg font-semibold">
                      {t.drawdown_age !== null ? `age ${age(t.drawdown_age)}` : "—"}
                    </div>
                    <div className={`text-[11px] font-mono ${solventClass(t.drawdown_p)}`}>
                      {t.drawdown_p !== null ? `${pct(t.drawdown_p)} solvent @95` : "no age clears"}
                    </div>
                  </td>
                  <td className="text-right">
                    <div className="font-mono text-lg font-semibold">
                      {t.preservation_age !== null ? `age ${age(t.preservation_age)}` : "—"}
                    </div>
                    <div className={`text-[11px] font-mono ${solventClass(t.preservation_p)}`}>
                      {t.preservation_p !== null
                        ? `${pct(t.preservation_p)} solvent @95`
                        : "no age preserves"}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* ---- Plain-English tradeoff line ---- */}
        {tDraw !== null && tPres !== null ? (
          <p className="mt-3 text-sm text-muted-foreground leading-relaxed">
            Retire as early as{" "}
            <span className="font-semibold text-foreground">{age(tDraw)}</span> spending
            down, or{" "}
            <span className="font-semibold text-foreground">{age(tPres)}</span> to
            preserve your principal for the kids — a{" "}
            <span className="font-semibold text-foreground">
              {tradeoffYears ?? 0}-year
            </span>{" "}
            tradeoff.
          </p>
        ) : (
          <p className="mt-3 text-sm text-muted-foreground leading-relaxed">
            Within the search horizon, the typical regime doesn&apos;t clear both
            bars — see the per-regime ages above and the assumptions below.
          </p>
        )}

        {/* ---- Estate frontier (typical track) ---- */}
        {frontier.length > 0 && (
          <div className="mt-5">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
              What you&apos;d leave, in today&apos;s money — typical regime
            </div>
            <ResponsiveContainer width="100%" height={240}>
              <ComposedChart
                data={frontier}
                margin={{ top: 8, right: 16, bottom: 4, left: 8 }}
              >
                <CartesianGrid strokeDasharray="3 3" opacity={0.18} />
                <XAxis
                  dataKey="retire_age"
                  type="number"
                  domain={["dataMin", "dataMax"]}
                  fontSize={11}
                  tickFormatter={(v) => `${Number(v).toFixed(0)}`}
                  label={{
                    value: "retire age",
                    position: "insideBottom",
                    offset: -2,
                    fontSize: 10,
                    fill: "currentColor",
                  }}
                />
                <YAxis
                  fontSize={10}
                  tickFormatter={(v) => nisCompact(Number(v))}
                  width={56}
                />
                <Tooltip
                  formatter={(value, name) => [nisFull(Number(value)), String(name)]}
                  labelFormatter={(label) => `retire at age ${label}`}
                  contentStyle={{
                    background: "var(--color-popover)",
                    border: "1px solid var(--color-border)",
                    borderRadius: 8,
                    fontSize: 12,
                  }}
                />
                <Area
                  type="monotone"
                  dataKey="median"
                  stroke="#6366f1"
                  strokeWidth={2}
                  fill="#6366f1"
                  fillOpacity={0.12}
                  name="Median estate"
                />
                <Line
                  type="monotone"
                  dataKey="worst10"
                  stroke="#f59e0b"
                  strokeWidth={2}
                  dot={false}
                  name="Worst-10% estate"
                />
                {tDraw !== null && (
                  <ReferenceLine
                    x={tDraw}
                    stroke="#34d399"
                    strokeWidth={1.5}
                    strokeDasharray="4 2"
                    label={{
                      value: `retire ${age(tDraw)}`,
                      position: "top",
                      fill: "#34d399",
                      fontSize: 10,
                    }}
                  />
                )}
                {tPres !== null && (
                  <ReferenceLine
                    x={tPres}
                    stroke="#a78bfa"
                    strokeWidth={1.5}
                    strokeDasharray="4 2"
                    label={{
                      value: `preserve ${age(tPres)}`,
                      position: "top",
                      fill: "#a78bfa",
                      fontSize: 10,
                    }}
                  />
                )}
              </ComposedChart>
            </ResponsiveContainer>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
              <span className="inline-flex items-center gap-1.5">
                <span className="inline-block h-2 w-3 rounded-sm bg-[#6366f1]/70" />
                Median estate (real)
              </span>
              <span className="inline-flex items-center gap-1.5">
                <span className="inline-block h-2 w-3 rounded-sm bg-[#f59e0b]" />
                Worst-10% estate (real)
              </span>
            </div>
          </div>
        )}

        {/* ---- Spend-to-retire-now callout ---- */}
        {data.spend_to_retire_now_nis !== null && (
          <div className="mt-5 rounded-md border border-info/30 bg-info/10 p-3 text-sm">
            <span className="font-medium text-foreground">To retire today</span> you&apos;d
            cap spending at{" "}
            <span className="font-mono font-semibold text-foreground">
              ~{nisFull(data.spend_to_retire_now_nis)}/yr
            </span>{" "}
            (vs{" "}
            <span className="font-mono">{nisFull(data.spend_central_nis)}</span>{" "}
            planned) — the spend-down lever that buys you the years.
          </div>
        )}

        {/* ---- Assumptions panel (show me the numbers) ---- */}
        <DrilldownSection title="Assumptions & derivation" defaultOpen={false}>
          <MethodologyPanel title="What drives these numbers">
            <p>Every figure below is Argosy-derived — no hardcoded magic numbers.</p>
            <ul className="list-disc pl-5">
              <li>
                <b>Real return (typical):</b>{" "}
                {pct(numAssumption(a, "mu_real_typical") ?? typical?.mu_real)} —
                the central regime; bull / bear bracket it (see table).
              </li>
              <li>
                <b>Interim withdrawal tax:</b>{" "}
                {pct(numAssumption(a, "withdrawal_tax"))} on the taxable-gain
                fraction of each sale.
              </li>
              <li>
                <b>Reserve PV discount:</b>{" "}
                {pct(numAssumption(a, "reserve_discount_real"))} real — the
                finite-liability reserve is present-valued at the safe rate, not
                the risky return.
              </li>
              <li>
                <b>Central spend:</b> {nisFull(data.spend_central_nis)}/yr —{" "}
                {strAssumption(a, "spend_central_source") ??
                  "the permanent-equivalent basis, healthcare included"}
                . Stress spend: {nisFull(data.spend_stress_nis)}/yr.
              </li>
              <li>
                <b>Drawdown bar:</b> P(solvent to 95) ≥{" "}
                {pct(numAssumption(a, "bar_drawdown"))}.
              </li>
              <li>
                <b>Preservation bar:</b> {pct(numAssumption(a, "bar_preservation"))}{" "}
                — tested as{" "}
                <i>
                  {strAssumption(a, "preservation_test") ??
                    "worst-10% real estate ≥ starting principal"}
                </i>
                .
              </li>
              <li>
                <b>Volatility (σ, current):</b> {pct(data.sigma_current)} annualized.
              </li>
              <li>
                <b>Monte-Carlo paths:</b>{" "}
                {(numAssumption(a, "n_paths") ?? 0).toLocaleString()} per age × regime.
              </li>
            </ul>
            <div className="mt-2">
              <div className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1">
                Deployable capital — full − CGT − reserve PV
              </div>
              <table className="w-full text-xs font-mono tabular-nums">
                <tbody>
                  <tr className="border-t border-border/40">
                    <td className="py-1 font-sans text-muted-foreground">Full portfolio</td>
                    <td className="py-1 text-right">{nisFull(data.full_portfolio_nis)}</td>
                  </tr>
                  <tr className="border-t border-border/40">
                    <td className="py-1 font-sans text-muted-foreground">
                      − Capital-gains haircut
                    </td>
                    <td className="py-1 text-right text-rose-400">
                      −{nisFull(data.cgt_haircut_nis)}
                    </td>
                  </tr>
                  <tr className="border-t border-border/40">
                    <td className="py-1 font-sans text-muted-foreground">
                      − Reserve (PV; raw {nisFull(data.reserve_raw_nis)})
                    </td>
                    <td className="py-1 text-right text-rose-400">
                      −{nisFull(data.reserve_pv_nis)}
                    </td>
                  </tr>
                  <tr className="border-t border-border/60">
                    <td className="py-1 font-sans font-semibold text-foreground">
                      = Deployable
                    </td>
                    <td className="py-1 text-right font-semibold text-foreground">
                      {nisFull(data.deployable_nis)}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>

            {/* Stress / FX context */}
            <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
              <span>
                Stress regime drawdown age:{" "}
                <span className="font-mono text-foreground">{age(data.stress_drawdown_age)}</span>
              </span>
              <span>
                Stress regime preservation age:{" "}
                <span className="font-mono text-foreground">
                  {age(data.stress_preservation_age)}
                </span>
              </span>
            </div>
            {data.fx_stress_band.length > 0 && (
              <div className="mt-2 text-xs text-muted-foreground">
                <span className="text-[10px] uppercase tracking-wider">FX stress band</span>
                <div className="mt-1 flex flex-wrap gap-2 font-mono">
                  {data.fx_stress_band.map((b) => (
                    <span
                      key={b.fx_adverse_pct}
                      className="rounded border border-border/40 px-2 py-0.5"
                    >
                      {pct(b.fx_adverse_pct)} adverse → retire {age(b.drawdown_age)}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </MethodologyPanel>
        </DrilldownSection>
      </CardContent>
    </Card>
  );
}

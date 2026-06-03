"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import { api } from "@/lib/api";

interface Props {
  userId: string;
}

interface Retirement {
  base: number | null;
  bear: number | null;
  bull: number | null;
  assumed: number;
  todayAge: number;
  // Sourced from the projection's `assumptions` so the scenario labels
  // describe what the engine actually computes (a ±1σ portfolio-value
  // band at the base real return) rather than fabricated return rates.
  realReturn: number; // real_return_annual, e.g. 0.055
  sigma: number; // sigma_annual, e.g. 0.18
}

/**
 * Expected retirement date — headline answer to "when can I retire?"
 *
 * Per the 2026-05-29 framing: /retirement is the visualization +
 * confirmation surface for the plan, and the single most important
 * number it should show is the actual answer to the retirement
 * question. P(solvent at 95) tells you "if I retire at age 49, will
 * the money last?" — but the user often asks the inverse: "WHEN
 * is the earliest I can retire?"
 *
 * The cashflow_projection engine already computes this as
 * retire_ready_age_{base,bear,bull}: the earliest month where
 * projected total monthly income (portfolio real return + pension
 * annuity) covers inflated expenses, under three return scenarios.
 * This card just surfaces that as the page's headline.
 */
export function ExpectedRetirementAgeCard({ userId }: Props) {
  const [data, setData] = useState<Retirement | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .planDraftCashflowProjection(userId)
      .then((d) => {
        if (cancelled) return;
        setData({
          base: d.retire_ready_age_base,
          bear: d.retire_ready_age_bear,
          bull: d.retire_ready_age_bull,
          assumed: d.retirement_age_assumed,
          todayAge: d.today_age_years,
          realReturn: d.assumptions.real_return_annual,
          sigma: d.assumptions.sigma_annual,
        });
      })
      .catch((e: unknown) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">When can I retire?</CardTitle>
          <CardDescription>Computing earliest feasible retirement age…</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  // 404 (no plan draft) — graceful empty-state. The /plan page is where
  // the projection actually lives; nudge the user there.
  if (err || data === null) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">When can I retire?</CardTitle>
          <CardDescription>
            Needs a current plan to compute. Head to{" "}
            <Link href="/plan" className="text-info hover:underline">/plan</Link>{" "}
            to generate or accept a draft, then this card will populate.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const base = data.base;
  const yearsAway = base !== null ? base - data.todayAge : null;
  // Tone reflects how soon: < 5 years = success, 5-15 = neutral,
  // > 15 = warning (retirement is still far out).
  const baseTone: "success" | "neutral" | "warning" =
    yearsAway === null
      ? "neutral"
      : yearsAway < 5
        ? "success"
        : yearsAway < 15
          ? "neutral"
          : "warning";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2 flex-wrap">
          When can I retire?
          <StatusPill tone={baseTone} mono>
            EARLIEST FEASIBLE
          </StatusPill>
        </CardTitle>
        <CardDescription>
          Earliest month where projected monthly income (portfolio real-return
          drawdown + pension annuity) covers inflated expenses. The base case
          uses the plan&apos;s {(data.realReturn * 100).toFixed(1)}% real return;
          the downside / upside columns apply a ±1σ (σ={(data.sigma * 100).toFixed(0)}%)
          portfolio-value band around it — they are not separate return
          assumptions. Driven by the cashflow projection on the current plan.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <AgeBlock
            label={`Base case (${(data.realReturn * 100).toFixed(1)}% real)`}
            age={base}
            todayAge={data.todayAge}
            prominent
          />
          <AgeBlock label="Downside (−1σ band)" age={data.bear} todayAge={data.todayAge} />
          <AgeBlock label="Upside (+1σ band)" age={data.bull} todayAge={data.todayAge} />
        </div>
        <div className="mt-3 rounded-md border border-border/60 bg-muted/30 p-3 text-[11px] leading-relaxed text-muted-foreground">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-foreground/80">
            Reconciling this with the plan&apos;s age {data.assumed}
          </div>
          {base !== null ? (
            <>
              The plan targets retirement at{" "}
              <span className="font-mono text-foreground">{data.assumed}</span>,
              but the projection says the base case is already feasible at{" "}
              <span className="font-mono text-emerald-400">{base.toFixed(1)}</span>
              {base <= data.todayAge + 0.25 ? " — i.e. today" : ""}.{" "}
              {base <= data.todayAge + 0.25 ? (
                <>
                  Portfolio real-return income plus pension annuity already
                  covers projected expenses at the current balance, so you clear
                  the feasibility bar now. The three columns read the same age
                  because the ±1σ band has zero spread at t=0 and the base case
                  is already past the threshold — not a display bug.{" "}
                </>
              ) : null}
              The ~{Math.max(0, data.assumed - base).toFixed(0)}-year gap between{" "}
              <span className="font-mono">{base.toFixed(1)}</span> and the
              plan&apos;s <span className="font-mono">{data.assumed}</span> is a
              margin, not a constraint — see{" "}
              <Link href="/plan" className="text-info hover:underline">/plan</Link>{" "}
              for the assumptions behind the target age.
            </>
          ) : (
            <>
              No base-case feasibility crossing inside the projection horizon —
              see{" "}
              <Link href="/plan" className="text-info hover:underline">/plan</Link>{" "}
              for the assumptions feeding this.
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

interface AgeBlockProps {
  label: string;
  age: number | null;
  todayAge: number;
  prominent?: boolean;
}

function AgeBlock({ label, age, todayAge, prominent }: AgeBlockProps) {
  const yearsAway = age !== null ? age - todayAge : null;
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div
        className={
          prominent
            ? "mt-1 text-3xl font-mono font-semibold tabular-nums"
            : "mt-1 text-2xl font-mono font-semibold tabular-nums text-muted-foreground"
        }
      >
        {age !== null ? `age ${age.toFixed(1)}` : "—"}
      </div>
      {yearsAway !== null ? (
        <div className="text-xs text-muted-foreground tabular-nums">
          {yearsAway >= 0
            ? `in ~${yearsAway.toFixed(1)} years`
            : `${Math.abs(yearsAway).toFixed(1)} years ago — already past`}
        </div>
      ) : (
        <div className="text-xs text-muted-foreground">
          no crossing in 40-year horizon
        </div>
      )}
    </div>
  );
}

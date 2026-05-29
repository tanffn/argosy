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
          Earliest month where projected total monthly income (portfolio real
          return + pension annuity) covers inflated expenses, under three
          return scenarios. Driven by the cashflow projection on the
          current plan draft.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <AgeBlock label="Base (4.5% real)" age={base} todayAge={data.todayAge} prominent />
          <AgeBlock label="Bear (0% real)" age={data.bear} todayAge={data.todayAge} />
          <AgeBlock label="Bull (6% real)" age={data.bull} todayAge={data.todayAge} />
        </div>
        <div className="mt-3 text-[11px] text-muted-foreground">
          Plan&apos;s assumed retirement age: <span className="font-mono">{data.assumed}</span>{" "}
          (vs base feasibility{" "}
          {base !== null ? (
            <span
              className={
                base <= data.assumed
                  ? "font-mono text-emerald-400"
                  : "font-mono text-amber-400"
              }
            >
              {base.toFixed(1)}
            </span>
          ) : (
            <span className="font-mono">—</span>
          )}
          ).{" "}
          {base !== null && base <= data.assumed ? (
            <>The plan&apos;s target age is reachable under the base scenario.</>
          ) : base !== null ? (
            <>The plan&apos;s target is earlier than what the base scenario supports — either delay retirement or push the savings rate.</>
          ) : (
            <>No base-scenario crossing inside the 40-year projection horizon — see /plan for the assumptions feeding this.</>
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

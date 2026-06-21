"use client";

import Link from "next/link";
import { Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import { api } from "@/lib/api";
import type { FeasibleAgeResponse } from "@/lib/retirement-types";

interface Props {
  userId: string;
}

/**
 * Expected retirement age — headline answer to "when can I retire?"
 *
 * Age-coherence (1b): this binds to the ONE canonical age — the earliest age
 * the base-case Monte Carlo clears the solvency bar (default 90%) with the
 * finite-liability reserve earmarked (sequence-of-returns aware). It REPLACES
 * the prior deterministic income-crossing reading, which reported the current
 * age (44) and contradicted the FI age + the MC. Shows the three labeled
 * anchors so no surface contradicts another: earliest-safe / operational-target
 * / statutory.
 */
export function ExpectedRetirementAgeCard({ userId }: Props) {
  const [data, setData] = useState<FeasibleAgeResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .feasibleAge(userId, { seed: 42 })
      .then((d) => {
        if (!cancelled) setData(d);
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
          <CardDescription className="flex items-center gap-2">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Running the readiness Monte Carlo…
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }
  if (err || !data) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">When can I retire?</CardTitle>
          <CardDescription className="text-rose-400">
            {err ?? "No plan yet — visit "}
            {!err && <Link href="/plan" className="text-info hover:underline">/plan</Link>}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const ef = data.earliest_feasible_age;
  const yearsAway = ef !== null ? ef - data.current_age : null;
  const tone: "success" | "neutral" | "warning" =
    yearsAway === null ? "warning" : yearsAway < 5 ? "success" : yearsAway < 15 ? "neutral" : "warning";
  const pct = (v: number) => `${(v * 100).toFixed(0)}%`;
  const nis = (v: number) => `₪${Math.round(v).toLocaleString()}`;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2 flex-wrap">
          When can I retire?
          <StatusPill tone={tone} mono>EARLIEST SAFE</StatusPill>
        </CardTitle>
        <CardDescription>
          The earliest age the base-case Monte Carlo clears{" "}
          {pct(data.target_p_solvent)} solvency to age 95 with the{" "}
          {nis(data.reserve_netted_nis)} finite-liability reserve earmarked —
          sequence-of-returns aware, Bituach Leumi credited, annuity taxed.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <AgeBlock
            label="Earliest safe (MC 90%, reserve-netted)"
            age={ef}
            todayAge={data.current_age}
            subtitle={data.p_solvent_at_age !== null ? `${pct(data.p_solvent_at_age)} solvent @95` : undefined}
            prominent
          />
          <AgeBlock label="Operational target (the plan)" age={data.operational_target_age} todayAge={data.current_age} />
          <AgeBlock
            label="Statutory (pension annuity + BL)"
            age={data.statutory_annuity_age}
            todayAge={data.current_age}
            subtitle={`lump from ${data.statutory_lump_age}`}
          />
        </div>
        <div className="mt-3 rounded-md border border-border/60 bg-muted/30 p-3 text-[11px] leading-relaxed text-muted-foreground">
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-foreground/80">
            Why not earlier?
          </div>
          {ef !== null ? (
            <>
              On current purchasing power the perpetuity base is crossed, but the{" "}
              {nis(data.reserve_netted_nis)} reserve (education, mortgage runoff,
              weddings) is earmarked — net of it you clear the{" "}
              {pct(data.target_p_solvent)} Monte-Carlo bar at age{" "}
              <span className="font-mono text-emerald-400">{ef.toFixed(0)}</span>,
              not your current age. This supersedes the old deterministic
              income-crossing reading. See{" "}
              <Link href="/plan" className="text-info hover:underline">/plan</Link>{" "}
              for the full derivation.
            </>
          ) : (
            <>
              The base case does not clear the {pct(data.target_p_solvent)} bar
              within the horizon at the reserve-netted portfolio — delay,
              de-risk, or raise the savings rate. See{" "}
              <Link href="/plan" className="text-info hover:underline">/plan</Link>.
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
  subtitle?: string;
  prominent?: boolean;
}

function AgeBlock({ label, age, todayAge, subtitle, prominent }: AgeBlockProps) {
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
        {age !== null ? `age ${age.toFixed(0)}` : "—"}
      </div>
      {subtitle ? (
        <div className="text-xs text-muted-foreground tabular-nums">{subtitle}</div>
      ) : yearsAway !== null ? (
        <div className="text-xs text-muted-foreground tabular-nums">
          {yearsAway >= 0 ? `in ~${yearsAway.toFixed(0)} years` : `${Math.abs(yearsAway).toFixed(0)} years ago`}
        </div>
      ) : (
        <div className="text-xs text-muted-foreground">not within horizon</div>
      )}
    </div>
  );
}

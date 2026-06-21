"use client";

import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ValueWithTooltip } from "@/components/retirement/ValueWithTooltip";
import { apiUrl } from "@/lib/api-base";
import type { ValueWithRationale } from "@/lib/retirement-types";

interface Alert {
  asset_class: string;
  current_pct: ValueWithRationale;
  target_pct: ValueWithRationale;
  drift_pp: ValueWithRationale;
  rule_fired: string;
  suggested_proposal: string;
}

interface Props {
  userId: string;
  currentAge: number;
}

export function RebalancingAlertsCard({ userId, currentAge }: Props) {
  const [data, setData] = useState<Alert[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(
      apiUrl(`/api/retirement/rebalancing-alerts?user_id=${encodeURIComponent(userId)}&current_age=${currentAge}`),
      { cache: "no-store" },
    )
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((d) => {
        if (!cancelled) setData(d.alerts);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [userId, currentAge]);

  if (err) {
    return (
      <Card>
        <CardHeader><CardTitle className="text-base">Rebalancing alerts</CardTitle><CardDescription className="text-rose-400">{err}</CardDescription></CardHeader>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          Rebalancing alerts
          <span className="text-xs font-mono text-muted-foreground">
            {(data?.length ?? 0) === 0 ? "ALIGNED" : `${data?.length} drift${data?.length === 1 ? "" : "s"}`}
          </span>
        </CardTitle>
        <CardDescription>
          5/25 rule + quarterly check. Fires when any asset class is &gt; 5pp
          off target or &gt; 25% relative drift.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {data === null ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
            Loading…
          </p>
        ) : data.length === 0 ? (
          <p className="text-sm text-emerald-400">
            ●ALIGNED — no rebalancing needed at age {currentAge}.
          </p>
        ) : (
          <ul className="space-y-3">
            {data.map((a) => {
              const drift = typeof a.drift_pp.value === "number" ? a.drift_pp.value : 0;
              const sign = drift >= 0 ? "+" : "−";
              return (
                <li key={a.asset_class} className="rounded-md border border-border/40 px-3 py-2">
                  <div className="text-sm font-medium capitalize">
                    {a.asset_class}{" "}
                    <span className="text-xs text-muted-foreground">
                      ({a.rule_fired.replace(/_/g, " ")})
                    </span>
                  </div>
                  <div className="text-xs text-muted-foreground mt-0.5">
                    actual{" "}
                    <ValueWithTooltip data={a.current_pct} />{" "}
                    · target{" "}
                    <ValueWithTooltip data={a.target_pct} />{" "}
                    · drift{" "}
                    <ValueWithTooltip data={a.drift_pp}>
                      {sign}
                      {Math.abs(drift).toFixed(1)} pp
                    </ValueWithTooltip>
                  </div>
                  <div className="mt-1 text-sm">{a.suggested_proposal}</div>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

"use client";

/**
 * Unified "Visualize your plan" panel.
 *
 * A single card with a shared selector — retire age (47 / 55) × market
 * (Bull / Typical / Bear) — that self-fetches the Monte Carlo series on
 * the DUAL-TRACK PLAN basis (deconcentrated NVDA, σ-glide 34→18%,
 * reserve-netted at PV, 5% real / 10% interim tax) from
 * ``/api/plan/current/plan-series``. The fetched response feeds the two
 * existing views — Portfolio bands + Cashflow coverage — rendered in a
 * tabbed layout so they share one selector instead of two standalone
 * charts on the stale "do nothing" config.
 */

import { useEffect, useState } from "react";

import { CashflowInflowOutflowChart } from "@/components/plan/cashflow-inflow-outflow-chart";
import { MonteCarloBandsChart } from "@/components/plan/monte-carlo-bands-chart";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, type MonteCarloProjectionResponse } from "@/lib/api";

type Regime = "bull" | "typical" | "bear";

const RETIRE_AGES: readonly number[] = [47, 55];
const REGIMES: readonly Regime[] = ["bull", "typical", "bear"];
const REGIME_LABEL: Record<Regime, string> = {
  bull: "Bull",
  typical: "Typical",
  bear: "Bear",
};

// 1200 paths keeps the per-request cost responsive while tightening the
// percentile bands enough that the verdict stat reads stably.
const N_PATHS = 1200;

function segBtn(active: boolean): string {
  return `px-3 py-1 text-xs font-medium font-mono transition-colors ${
    active
      ? "bg-primary text-primary-foreground"
      : "bg-transparent text-muted-foreground hover:bg-muted/50"
  }`;
}

export function PlanVisualization({ userId }: { userId: string }) {
  const [retireAge, setRetireAge] = useState<number>(47);
  const [regime, setRegime] = useState<Regime>("typical");
  const [response, setResponse] = useState<MonteCarloProjectionResponse | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- justified: selection-driven fetch; toggling loading/error inside the effect is the whole point
    setLoading(true);
    setError(null);
    api
      .planSeries(userId, {
        retire_age: retireAge,
        regime,
        n_paths: N_PATHS,
      })
      .then((r) => {
        if (cancelled) return;
        setResponse(r);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setResponse(null);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId, retireAge, regime]);

  return (
    <Card data-slot="plan-visualization">
      <CardHeader>
        <CardTitle className="text-base">Visualize your plan</CardTitle>
        <CardDescription>
          Both views run the Monte Carlo on your{" "}
          <strong>actual plan basis</strong> — NVDA deconcentrated to its
          strategic cap, volatility gliding down as you diversify, the
          reserve held aside at present value, a 5% real / ≈7.5% nominal
          central return and a 10% interim withdrawal tax. Pick a retire
          age and a market regime; the bands and the cashflow coverage
          update together.
        </CardDescription>
        <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-2">
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-muted-foreground">Retire age</span>
            <div className="inline-flex overflow-hidden rounded-md border border-border/60">
              {RETIRE_AGES.map((a) => (
                <button
                  key={a}
                  type="button"
                  onClick={() => setRetireAge(a)}
                  className={segBtn(retireAge === a)}
                >
                  {a}
                </button>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-muted-foreground">Market</span>
            <div className="inline-flex overflow-hidden rounded-md border border-border/60">
              {REGIMES.map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRegime(r)}
                  className={segBtn(regime === r)}
                >
                  {REGIME_LABEL[r]}
                </button>
              ))}
            </div>
          </div>
          {loading ? (
            <span className="text-[11px] text-muted-foreground italic">
              Running {N_PATHS.toLocaleString()} simulations…
            </span>
          ) : null}
        </div>
      </CardHeader>
      <CardContent>
        {error ? (
          <p className="text-sm text-error font-mono">
            Projection unavailable: {error}
          </p>
        ) : loading && response == null ? (
          <p className="text-sm text-muted-foreground">
            Running {N_PATHS.toLocaleString()} simulations on the plan
            basis…
          </p>
        ) : (
          <Tabs defaultValue="bands">
            <TabsList>
              <TabsTrigger value="bands">Portfolio bands</TabsTrigger>
              <TabsTrigger value="cashflow">Cashflow coverage</TabsTrigger>
            </TabsList>
            <TabsContent value="bands">
              <MonteCarloBandsChart response={response} />
            </TabsContent>
            <TabsContent value="cashflow">
              <CashflowInflowOutflowChart response={response} />
            </TabsContent>
          </Tabs>
        )}
      </CardContent>
    </Card>
  );
}

"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type HighPotentialSleeveDTO } from "@/lib/api";

interface Props {
  /** Cash being redeployed; the sleeve is sleevePct of this. */
  cashUsd?: number;
  /** High-potential share of the redeployed cash (default 5%). */
  sleevePct?: number;
}

function convictionTone(c: string): "success" | "secondary" | "outline" {
  if (c === "HIGH") return "success";
  if (c === "LOW") return "outline";
  return "secondary";
}

function vehicleLabel(v: string): string {
  return v === "ucits_thematic" ? "UCITS thematic (non-US-situs)" : "Single name";
}

/**
 * /proposals + /portfolio tile: the med-high-risk "high-potential" sleeve the
 * user asked to carve out of a cash deployment (≥5% of redeployed cash).
 *
 * Blend vehicle: a UCITS thematic core (Irish-domiciled, NOT US-situs — keeps
 * the sleeve off the estate-tax base) + a single-name carve-out (US-situs;
 * estate-tax accepted on that small slice). Each candidate shows conviction,
 * % of the sleeve, $ size, and the buy thesis. Seeds are the advisor's first
 * pass; the agent fleet validates + final-sizes on the next synth.
 */
export function HighPotentialSleeveCard({
  cashUsd = 250_000,
  sleevePct = 5.0,
}: Props) {
  const [data, setData] = useState<HighPotentialSleeveDTO | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .portfolioHighPotentialSleeve(cashUsd, sleevePct)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [cashUsd, sleevePct]);

  if (error || data === null) return null;
  if (data.candidates.length === 0) return null;

  const ucitsPct = data.vehicle_split["ucits_thematic"] ?? 0;
  const singlePct = data.vehicle_split["single_name"] ?? 0;

  return (
    <Card className="border-info/30">
      <CardHeader>
        <div className="flex items-start justify-between gap-2 flex-wrap">
          <div>
            <CardTitle className="text-base font-mono">
              High-potential sleeve &mdash; ${(data.sleeve_budget_usd / 1000).toFixed(1)}K
            </CardTitle>
            <CardDescription className="mt-1">
              {data.sleeve_pct_of_cash.toFixed(0)}% of a $
              {(data.cash_basis_usd / 1000).toFixed(0)}K cash deployment, conviction-weighted.
              Blend: {ucitsPct.toFixed(0)}% UCITS thematic core (non-US-situs) /{" "}
              {singlePct.toFixed(0)}% single-name carve-out (US-situs &mdash; estate-tax on
              this slice only).
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {data.candidates.map((c) => (
          <div
            key={c.ticker}
            className="rounded-md border border-border bg-secondary/30 px-3 py-2 text-xs"
          >
            <div className="flex items-center gap-2 flex-wrap font-mono">
              <span className="font-semibold text-sm">{c.ticker}</span>
              <span className="text-muted-foreground">{c.name}</span>
              <Badge variant={convictionTone(c.conviction)}>{c.conviction}</Badge>
              <Badge variant="outline">{vehicleLabel(c.vehicle)}</Badge>
              <span className="text-foreground">
                {c.pct_of_sleeve.toFixed(0)}% &middot; ${(c.amount_usd / 1000).toFixed(1)}K
              </span>
              {c.held_today && (
                <Badge variant="secondary" className="text-[10px]">
                  already held
                </Badge>
              )}
              {c.us_situs && (
                <Badge variant="outline" className="text-[10px]" title="US-situs: adds US estate-tax exposure">
                  US-situs
                </Badge>
              )}
            </div>
            <div className="mt-1 text-muted-foreground leading-relaxed">
              {c.thesis}
            </div>
          </div>
        ))}
        <div className="mt-2 text-[11px] text-muted-foreground">{data.note}</div>
      </CardContent>
    </Card>
  );
}

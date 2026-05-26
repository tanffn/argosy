"use client";

import { useMemo } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { DraftResponse } from "@/lib/api";

// Categories the heatmap columns represent. Order matches the canonical
// agent fleet so the heatmap reads left-to-right as the synthesis pipeline.
const CATEGORIES = [
  "user_context",
  "FundamentalsAnalyst",
  "TechnicalAnalyst",
  "NewsAnalyst",
  "MacroAnalyst",
  "FXAnalyst",
  "TaxAnalyst",
  "ConcentrationAnalyst",
  "SentimentAnalyst",
] as const;

type Category = (typeof CATEGORIES)[number];

interface Row {
  horizon: "long" | "medium" | "short";
  item_id: string;
  summary: string;
  counts: Record<Category, number>;
  total: number;
}

function shortLabel(cat: Category): string {
  if (cat === "user_context") return "user_ctx";
  return cat.replace(/Analyst$/, "");
}

function cellColor(count: number, max: number): string {
  if (count === 0 || max === 0) return "bg-muted/20";
  const ratio = count / max;
  // Use Tailwind opacity tiers for accessibility-friendly contrast.
  if (ratio < 0.25) return "bg-primary/20";
  if (ratio < 0.5) return "bg-primary/40";
  if (ratio < 0.75) return "bg-primary/60";
  return "bg-primary/80";
}

interface SourcesHeatmapProps {
  draft: DraftResponse;
}

export function SourcesHeatmap(props: SourcesHeatmapProps) {
  const { draft } = props;

  const rows = useMemo(() => {
    const out: Row[] = [];
    const horizons = [
      ["long", draft.horizon_long],
      ["medium", draft.horizon_medium],
      ["short", draft.horizon_short],
    ] as const;
    for (const [horizon, hv] of horizons) {
      if (!hv) continue;
      for (const d of hv.deltas_from_prior) {
        const counts: Record<Category, number> = {
          user_context: 0,
          FundamentalsAnalyst: 0,
          TechnicalAnalyst: 0,
          NewsAnalyst: 0,
          MacroAnalyst: 0,
          FXAnalyst: 0,
          TaxAnalyst: 0,
          ConcentrationAnalyst: 0,
          SentimentAnalyst: 0,
        };
        for (const label of d.provenance_agent_labels ?? []) {
          if ((CATEGORIES as readonly string[]).includes(label)) {
            counts[label as Category] += 1;
          }
        }
        // Also weight cited_sources directly so items with multiple cites
        // from the same agent show higher density.
        for (const src of d.cited_sources) {
          if (src.startsWith("user_context")) counts.user_context += 1;
          else if (src.startsWith("fundamentals/")) counts.FundamentalsAnalyst += 1;
          else if (src.startsWith("technical/")) counts.TechnicalAnalyst += 1;
          else if (src.startsWith("news/")) counts.NewsAnalyst += 1;
          else if (src.startsWith("macro/")) counts.MacroAnalyst += 1;
          else if (src.startsWith("fx/")) counts.FXAnalyst += 1;
          else if (src.startsWith("tax/")) counts.TaxAnalyst += 1;
          else if (src.startsWith("concentration/")) counts.ConcentrationAnalyst += 1;
          else if (src.startsWith("sentiment/")) counts.SentimentAnalyst += 1;
        }
        out.push({
          horizon,
          item_id: d.item_id,
          summary: d.summary,
          counts,
          total: Object.values(counts).reduce((s, v) => s + v, 0),
        });
      }
    }
    return out;
  }, [draft]);

  const maxCellCount = useMemo(() => {
    let m = 0;
    for (const r of rows) {
      for (const c of CATEGORIES) {
        if (r.counts[c] > m) m = r.counts[c];
      }
    }
    return m;
  }, [rows]);

  if (rows.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Cited sources by item</CardTitle>
        <CardDescription>
          Stronger cell color = more citations of that agent for that delta.
          Thin rows (mostly empty) flag weakly-grounded items.
        </CardDescription>
      </CardHeader>
      <CardContent className="overflow-x-auto">
        <table className="text-[10px] font-mono min-w-full">
          <thead>
            <tr className="text-muted-foreground">
              <th className="text-left py-1 pr-2 sticky left-0 bg-background">
                item
              </th>
              {CATEGORIES.map((c) => (
                <th
                  key={c}
                  className="text-center py-1 px-1 whitespace-nowrap"
                  title={c}
                >
                  {shortLabel(c)}
                </th>
              ))}
              <th className="text-right py-1 pl-2">total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.horizon}-${r.item_id}`} className="border-t border-border/20">
                <td
                  className="py-1 pr-2 max-w-[280px] truncate sticky left-0 bg-background"
                  title={r.summary}
                >
                  <span className="uppercase text-[9px] text-muted-foreground mr-1">
                    {r.horizon[0].toUpperCase()}
                  </span>
                  {r.summary}
                </td>
                {CATEGORIES.map((c) => {
                  const count = r.counts[c];
                  return (
                    <td
                      key={c}
                      className={`text-center px-1 py-1 ${cellColor(count, maxCellCount)}`}
                      title={`${c}: ${count}`}
                    >
                      {count > 0 ? count : ""}
                    </td>
                  );
                })}
                <td className="text-right pl-2 text-muted-foreground">
                  {r.total}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

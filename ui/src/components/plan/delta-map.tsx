"use client";

import { useMemo } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { DeltaItem, DraftResponse } from "@/lib/api";

const HORIZONS = ["long", "medium", "short"] as const;
const KINDS = ["added", "modified", "removed"] as const;
type Horizon = (typeof HORIZONS)[number];
type Kind = (typeof KINDS)[number];

function kindClasses(kind: Kind): { bg: string; text: string } {
  switch (kind) {
    case "added":
      return { bg: "bg-success/20", text: "text-success" };
    case "modified":
      return { bg: "bg-secondary/30", text: "text-foreground" };
    case "removed":
      return { bg: "bg-error/20", text: "text-error" };
  }
}

interface DeltaMapProps {
  draft: DraftResponse;
}

export function DeltaMap(props: DeltaMapProps) {
  const { draft } = props;

  // grid[horizon][kind] = DeltaItem[]
  const grid = useMemo(() => {
    const g: Record<Horizon, Record<Kind, DeltaItem[]>> = {
      long: { added: [], modified: [], removed: [] },
      medium: { added: [], modified: [], removed: [] },
      short: { added: [], modified: [], removed: [] },
    };
    for (const [h, hv] of [
      ["long", draft.horizon_long],
      ["medium", draft.horizon_medium],
      ["short", draft.horizon_short],
    ] as const) {
      if (!hv) continue;
      for (const d of hv.deltas_from_prior) {
        g[h][d.change_kind].push(d);
      }
    }
    return g;
  }, [draft]);

  const total =
    grid.long.added.length +
    grid.long.modified.length +
    grid.long.removed.length +
    grid.medium.added.length +
    grid.medium.modified.length +
    grid.medium.removed.length +
    grid.short.added.length +
    grid.short.modified.length +
    grid.short.removed.length;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Delta map</CardTitle>
        <CardDescription>
          {total === 0
            ? "No changes proposed."
            : `${total} change${total === 1 ? "" : "s"} across 3 horizons.`}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-[10px] font-mono uppercase tracking-wide text-muted-foreground">
                <th className="text-left py-1.5 pr-2">horizon</th>
                {KINDS.map((k) => (
                  <th key={k} className="text-left py-1.5 px-2">
                    {k}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {HORIZONS.map((h) => (
                <tr key={h} className="border-t border-border/30">
                  <td className="py-2 pr-2 font-mono uppercase text-[10px] text-muted-foreground align-top">
                    {h}
                  </td>
                  {KINDS.map((k) => {
                    const items = grid[h][k];
                    const cls = kindClasses(k);
                    return (
                      <td key={k} className="py-2 px-2 align-top">
                        {items.length === 0 ? (
                          <span className="text-muted-foreground/40">—</span>
                        ) : (
                          <div className="flex flex-col gap-1">
                            <span
                              className={`inline-block ${cls.bg} ${cls.text} rounded px-1.5 py-0.5 font-mono text-[10px] w-fit`}
                              title={`${items.length} ${k} item(s)`}
                            >
                              {items.length}
                            </span>
                            <ul className="text-[11px] leading-snug">
                              {items.slice(0, 3).map((d) => (
                                <li
                                  key={d.item_id}
                                  className="truncate"
                                  title={d.summary}
                                >
                                  {d.summary}
                                </li>
                              ))}
                              {items.length > 3 && (
                                <li className="text-muted-foreground">
                                  + {items.length - 3} more
                                </li>
                              )}
                            </ul>
                          </div>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

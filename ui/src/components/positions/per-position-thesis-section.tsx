"use client";

import { useEffect, useMemo, useState } from "react";

import { PositionCard } from "@/components/positions/position-card";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type PositionThesisDTO } from "@/lib/api";

type VerdictFilter = "ALL" | PositionThesisDTO["verdict"];

const VERDICT_ORDER: VerdictFilter[] = [
  "ALL",
  "SELL",
  "TRIM",
  "HOLD",
  "BUY",
  "ADD",
];

interface PerPositionThesisSectionProps {
  userId: string;
  /**
   * When true, render a section heading. When the section is embedded
   * inside another page (e.g. /portfolio) the caller controls the
   * heading; the standalone /positions page disables this and provides
   * its own page-level <h1>.
   */
  withHeading?: boolean;
}

/**
 * Per-position thesis cards — one card per holding with
 * Hold/Buy/Trim/Sell verdict + conviction + reasoning + cited
 * sources (T4.1).
 *
 * Extracted from the standalone /positions page so the same block can
 * be embedded inside the Portfolio page. The standalone /positions
 * route is kept as a permalink and renders this same section.
 *
 * Server derivation is pure-Python: see
 * argosy/services/per_position_thesis.py.
 */
export function PerPositionThesisSection({
  userId,
  withHeading = false,
}: PerPositionThesisSectionProps) {
  const [theses, setTheses] = useState<PositionThesisDTO[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<VerdictFilter>("ALL");

  useEffect(() => {
    let cancelled = false;
    // We intentionally do NOT call setLoading(true) here — the initial
    // useState value already has loading=true, and React's
    // react-hooks/set-state-in-effect rule flags synchronous setState
    // inside the effect body. The setLoading(false) call in .finally()
    // is allowed (async, fired after the promise settles).
    api
      .positionTheses(userId)
      .then((data) => {
        if (!cancelled) setTheses(data);
      })
      .catch((e: unknown) => {
        if (!cancelled)
          setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const counts = useMemo(() => {
    const out: Record<PositionThesisDTO["verdict"], number> = {
      HOLD: 0,
      BUY: 0,
      TRIM: 0,
      SELL: 0,
      ADD: 0,
    };
    for (const t of theses ?? []) out[t.verdict] += 1;
    return out;
  }, [theses]);

  const filtered = useMemo(() => {
    if (filter === "ALL") return theses ?? [];
    return (theses ?? []).filter((t) => t.verdict === filter);
  }, [theses, filter]);

  return (
    <section className="flex flex-col gap-4">
      {withHeading && (
        <header>
          <h2 className="text-xl font-semibold tracking-tight">
            Per-position thesis
          </h2>
          <p className="text-sm text-muted-foreground">
            One card per holding · verdict + conviction + reasoning ·{" "}
            derived from the pending plan draft (or accepted plan when no
            draft is in flight).
          </p>
        </header>
      )}

      {loading && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}
      {error && (
        <p className="text-sm text-error font-mono">{error}</p>
      )}

      {!loading && theses !== null && theses.length === 0 && (
        <Card>
          <CardHeader>
            <CardTitle>No positions to thesis-check</CardTitle>
            <CardDescription>
              Either no plan is imported yet, or the portfolio snapshot
              is empty. Run synthesis from the{" "}
              <a href="/plan" className="text-primary hover:underline">
                /plan
              </a>{" "}
              page to generate a draft.
            </CardDescription>
          </CardHeader>
        </Card>
      )}

      {theses !== null && theses.length > 0 && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            {VERDICT_ORDER.map((v) => {
              const isActive = filter === v;
              const n =
                v === "ALL"
                  ? theses.length
                  : counts[v as PositionThesisDTO["verdict"]];
              if (v !== "ALL" && n === 0) return null;
              return (
                <button
                  key={v}
                  type="button"
                  onClick={() => setFilter(v)}
                  className={
                    isActive
                      ? "rounded-md border border-foreground/40 px-2 py-1 text-xs font-mono bg-secondary"
                      : "rounded-md border border-border px-2 py-1 text-xs font-mono hover:bg-secondary/40"
                  }
                >
                  {v} <span className="text-muted-foreground">({n})</span>
                </button>
              );
            })}
            <span className="ml-auto text-xs text-muted-foreground">
              {counts.ADD > 0 && (
                <>
                  <Badge variant="info" className="mr-1">
                    {counts.ADD}
                  </Badge>
                  replacement candidate
                  {counts.ADD === 1 ? "" : "s"} suggested
                </>
              )}
            </span>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            {filtered.map((t) => (
              <PositionCard key={`${t.verdict}-${t.ticker}`} thesis={t} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}

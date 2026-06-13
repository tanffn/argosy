"use client";

import { useCallback, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import { api, type GenerateTsvResponse } from "@/lib/api";

interface Props {
  userId: string;
  onGenerated?: (resp: GenerateTsvResponse) => void;
  /** Render without the outer Card chrome, for composing inside a shared
   *  "Update portfolio data" panel alongside the upload tile. */
  embedded?: boolean;
}

/**
 * Argosy-generates-the-TSV tile (2026-05-29).
 *
 * Button-driven refresh of the canonical Family Finances Status TSV.
 * Position structure carries forward from the most recent prior TSV at
 * the scan root; Leumi NIS + USD cash rows are overridden with the
 * latest closing balances from expense_statements; snapshot_date bumps
 * to today; Current-allocation block currents recompute against the
 * new totals.
 *
 * Sits as a sibling to the upload tile: upload is the input flow,
 * generate is the "compose latest state into a fresh TSV" flow. Both
 * write to the scan root with the canonical filename.
 */
export function GenerateTsvCard({ userId, onGenerated, embedded = false }: Props) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<GenerateTsvResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleClick = useCallback(async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const resp = await api.portfolioGenerateTsv(userId);
      setResult(resp);
      onGenerated?.(resp);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [userId, onGenerated]);

  const body = (
    <>
        <p className="text-sm text-muted-foreground mb-3">
          Composes a fresh{" "}
          <code className="font-mono">Family Finances Status</code> TSV
          from current Argosy state: position structure from the most
          recent prior TSV, Leumi NIS + USD cash rows from the latest
          bank-statement closing balances, allocation-block currents
          recomputed against the new totals. Writes to{" "}
          <code className="font-mono">$ARGOSY_EXPENSE_SAMPLES_ROOT</code>{" "}
          with today&apos;s canonical filename.
        </p>
        <Button onClick={handleClick} disabled={busy}>
          {busy ? "Generating…" : "Generate TSV now"}
        </Button>

        {error ? (
          <div className="mt-3 text-sm text-rose-400 font-mono">
            {error}
          </div>
        ) : null}

        {result ? (
          <div className="mt-3 space-y-2 font-mono text-xs">
            {result.tsv_persisted ? (
              <div className="rounded-md border border-emerald-400/30 bg-emerald-400/5 px-3 py-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <StatusPill tone="success" mono>
                    GENERATED
                  </StatusPill>
                  <span className="text-emerald-400">
                    Snapshot {result.snapshot_date}
                  </span>
                </div>
                <div className="mt-1 text-[11px] text-muted-foreground truncate">
                  {result.persisted_path}
                </div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-[11px]">
                  <div>
                    <span className="text-muted-foreground">Leumi NIS cash:</span>{" "}
                    {result.leumi_nis_cash !== null
                      ? `₪${result.leumi_nis_cash.toLocaleString()}`
                      : "—"}
                  </div>
                  <div>
                    <span className="text-muted-foreground">Leumi USD cash:</span>{" "}
                    {result.leumi_usd_cash !== null
                      ? `$${result.leumi_usd_cash.toLocaleString()}`
                      : "—"}
                  </div>
                </div>
              </div>
            ) : (
              <div className="rounded-md border border-rose-400/40 bg-rose-400/5 px-3 py-2 text-rose-400">
                <div className="font-semibold mb-1">Generate failed</div>
                <div>{result.detail ?? "unknown error"}</div>
              </div>
            )}
            {result.warnings.length > 0 ? (
              <ul className="rounded-md border border-warning/40 bg-warning/5 px-3 py-2 list-disc list-inside">
                {result.warnings.map((w, i) => (
                  <li key={i} className="text-warning text-[11px]">
                    {w}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
    </>
  );

  const heading = (
    <div className="flex items-center justify-between gap-2 flex-wrap">
      <span className="text-base font-mono font-medium">
        Generate latest TSV from Argosy state
      </span>
      <span className="text-[11px] text-muted-foreground">
        positions carried forward &middot; cash refreshed from Leumi statements
      </span>
    </div>
  );

  if (embedded) {
    return (
      <section className="space-y-3">
        {heading}
        {body}
      </section>
    );
  }

  return (
    <Card>
      <CardHeader>{heading}</CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
}

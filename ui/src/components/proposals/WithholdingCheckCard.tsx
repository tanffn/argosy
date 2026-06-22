"use client";

import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type WithholdingCheckResponse } from "@/lib/api";

const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

function fmtNis(n: number | null | undefined): string {
  return n === null || n === undefined
    ? "—"
    : `₪${Math.round(n).toLocaleString()}`;
}

function fmtPeriod(year: number | null, month: number | null): string {
  if (!year || !month) return "—";
  const m = MONTHS[month - 1] ?? String(month);
  return `${m} ${year}`;
}

type StatusMeta = {
  variant: "success" | "warning" | "error" | "info";
  label: string;
};

function statusMeta(status: string): StatusMeta {
  switch (status) {
    case "reconciled":
      return { variant: "success", label: "Reconciled" };
    case "discrepancy":
      return { variant: "error", label: "Discrepancy — investigate" };
    case "low_confidence":
      return { variant: "warning", label: "Low-confidence parse" };
    case "no_equity_yet":
      return { variant: "info", label: "No equity yet" };
    case "no_data":
    default:
      return { variant: "info", label: "No payslip yet" };
  }
}

function VerdictBody({ data }: { data: WithholdingCheckResponse }) {
  const v = data.verdict;
  if (!data.has_verdict || !v) {
    return (
      <p className="text-sm text-muted-foreground">
        No payslip has been ingested yet, so Argosy cannot verify the §102 RSU
        withholding. It checks automatically each day a new payslip is
        available.
      </p>
    );
  }

  const topup = v.potential_filing_topup;
  const hasTopup = topup !== null && topup !== undefined && topup > 0;

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm">{v.summary}</p>

      {(v.actual_tax_withheld !== null ||
        v.equity_ordinary_base !== null ||
        v.equity_capital_base !== null) && (
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
          <Figure label="Equity tax accounted (YTD)" value={fmtNis(v.actual_tax_withheld)} />
          <Figure label="Expected (§102 model)" value={fmtNis(v.expected_at_wire_rate)} />
          <Figure label="Reconciliation residual" value={fmtNis(v.reconc_residual)} />
          <Figure label="Ordinary equity base" value={fmtNis(v.equity_ordinary_base)} />
          <Figure label="Capital equity base" value={fmtNis(v.equity_capital_base)} />
          <Figure
            label="Effective rate"
            value={v.effective_rate_pct === null ? "—" : `${v.effective_rate_pct}%`}
          />
        </div>
      )}

      {hasTopup ? (
        <div className="rounded border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
          <span className="font-medium">Set aside ~{fmtNis(topup)}</span> for a
          possible filing-time top-up (the ordinary band was accounted at ~50%;
          the conservative estimate is ~62% if your marginal rate is the top
          bracket).
        </div>
      ) : (
        v.status === "reconciled" && (
          <div className="rounded border border-emerald-500/40 bg-emerald-500/5 p-3 text-sm">
            Adequate — the conservative filing estimate does not exceed what was
            already accounted; a refund (paid back through payroll) is even
            possible.
          </div>
        )
      )}

      {v.caveats.length > 0 && (
        <details className="text-xs text-muted-foreground">
          <summary className="cursor-pointer select-none">
            Caveats &amp; scope ({v.caveats.length})
          </summary>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            {v.caveats.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function Figure({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="font-mono font-medium">{value}</span>
    </div>
  );
}

export function WithholdingCheckCard({ userId }: { userId: string }) {
  const [data, setData] = useState<WithholdingCheckResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Manual refetch for the "Re-check" button. setState here is in an event
  // callback (not an effect body), so it's fine.
  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.taxWithholdingCheck(userId));
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [userId]);

  // On mount / userId change: fetch once, with a cancelled guard (the
  // project's accepted mount-fetch pattern; see unallocated-cash-card.tsx).
  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- intentional: show the spinner before each refetch.
    setLoading(true);
    api
      .taxWithholdingCheck(userId)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const meta = data ? statusMeta(data.status) : null;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <div className="flex items-center gap-2">
              <CardTitle className="text-base">
                RSU tax withholding — verified by Argosy
              </CardTitle>
              {meta && <Badge variant={meta.variant}>{meta.label}</Badge>}
            </div>
            <CardDescription className="max-w-3xl mt-1">
              Argosy checks the §102 equity tax accounted through your latest
              payslip against its own model — so you don&apos;t have to.
              {data?.has_verdict && (
                <>
                  {" "}
                  Based on {fmtPeriod(data.period_year, data.period_month)}.
                </>
              )}
            </CardDescription>
          </div>
          <Button size="sm" variant="outline" onClick={load} disabled={loading}>
            {loading ? "Checking…" : "Re-check"}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {error && <p className="text-sm text-error font-mono">{error}</p>}
        {data && <VerdictBody data={data} />}
        {!data && !error && (
          <p className="text-sm text-muted-foreground">Loading…</p>
        )}
      </CardContent>
    </Card>
  );
}

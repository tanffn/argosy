"use client";

import { useEffect, useState } from "react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type InstrumentClassListDTO } from "@/lib/api";

/**
 * Owner-visible instrument→plan-class map (Block H). Reassign persists
 * source=owner and outranks fleet. Unmapped held symbols fail loud.
 */
export function InstrumentClassMapCard({ userId = "ariel" }: { userId?: string }) {
  const [data, setData] = useState<InstrumentClassListDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const refresh = () => {
    api
      .instrumentClasses(userId)
      .then(setData)
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : String(e)),
      );
  };

  useEffect(() => {
    refresh();
  }, [userId]);

  async function seed() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.instrumentClassesSeed(userId);
      setMsg(
        `Seeded plan=${r.plan}, known-holdings=${r.fleet_deterministic}` +
          (r.unmapped_held.length
            ? ` · unmapped: ${r.unmapped_held.join(", ")}`
            : " · no unmapped"),
      );
      refresh();
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function reassign(symbol: string, label: string) {
    setBusy(true);
    try {
      await api.instrumentClassReassign(symbol, {
        user_id: userId,
        plan_class_label: label,
      });
      refresh();
    } catch (e: unknown) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Instrument → plan class</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-destructive">{error}</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle>Instrument → plan class</CardTitle>
            <CardDescription>
              One map for Sleeve column, allocation-vs-target, and deploy gaps.
              Owner edits outrank fleet; plan instruments always win at resolve.
            </CardDescription>
          </div>
          <button
            type="button"
            disabled={busy}
            onClick={() => void seed()}
            className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-secondary/50 disabled:opacity-50"
          >
            {busy ? "Working…" : "Seed map now"}
          </button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {msg && <p className="text-xs text-muted-foreground">{msg}</p>}
        {data?.unmapped_held && data.unmapped_held.length > 0 && (
          <div
            className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs"
            role="alert"
          >
            <span className="font-semibold">Unmapped — needs classification: </span>
            {data.unmapped_held.join(", ")}
          </div>
        )}
        {!data ? (
          <p className="text-xs text-muted-foreground">Loading…</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-muted-foreground border-b border-border">
                  <th className="py-1.5 pr-2">Symbol</th>
                  <th className="py-1.5 pr-2">Stored class</th>
                  <th className="py-1.5 pr-2">Source</th>
                  <th className="py-1.5 pr-2">Resolved</th>
                  <th className="py-1.5">Reassign</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((r) => (
                  <tr key={r.symbol} className="border-b border-border/40 align-top">
                    <td className="py-1.5 pr-2 font-mono font-medium">{r.symbol}</td>
                    <td className="py-1.5 pr-2">{r.plan_class_label}</td>
                    <td className="py-1.5 pr-2 text-muted-foreground">{r.source}</td>
                    <td className="py-1.5 pr-2">{r.resolved_label}</td>
                    <td className="py-1.5">
                      <select
                        className="max-w-[14rem] rounded border border-border bg-background px-1 py-0.5"
                        disabled={busy}
                        defaultValue=""
                        onChange={(e) => {
                          const v = e.target.value;
                          if (v) void reassign(r.symbol, v);
                          e.target.value = "";
                        }}
                      >
                        <option value="">—</option>
                        {data.plan_classes.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.rows.length === 0 && (
              <p className="text-xs text-muted-foreground mt-2">
                Map empty — click &quot;Seed map now&quot;.
              </p>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

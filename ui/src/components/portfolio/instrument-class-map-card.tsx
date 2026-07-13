"use client";

import { useEffect, useState } from "react";

import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { api, type InstrumentClassListDTO } from "@/lib/api";

/**
 * Owner-visible instrument→plan-class map (Block H). Same CollapsibleSection
 * shell as Per-position thesis — collapsed by default; body shows only
 * unassigned held symbols. Pick a class, then CONFIRM. When none remain,
 * "+" lets the owner pick any mapped symbol to reassign.
 */
export function InstrumentClassMapCard({ userId = "ariel" }: { userId?: string }) {
  const [data, setData] = useState<InstrumentClassListDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [addSymbol, setAddSymbol] = useState("");
  const [addClass, setAddClass] = useState("");
  /** Draft class picks keyed by symbol — apply only on CONFIRM. */
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const refresh = () => {
    api
      .instrumentClasses(userId)
      .then((d) => {
        setData(d);
        setAdding(false);
        setAddSymbol("");
        setAddClass("");
        setDrafts({});
      })
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
    setMsg(null);
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

  const unmapped = data?.unmapped_held ?? [];
  const summary = error
    ? "error"
    : !data
      ? "…"
      : unmapped.length > 0
        ? `${unmapped.length} unassigned`
        : "all assigned";

  return (
    <CollapsibleSection title="Instrument → plan class" summary={summary}>
      {error ? (
        <p className="text-xs text-destructive">{error}</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              Unassigned held symbols only. Owner edits outrank fleet; plan
              instruments always win at resolve.
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={() => void seed()}
              className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-secondary/50 disabled:opacity-50 shrink-0"
            >
              {busy ? "Working…" : "Seed map now"}
            </button>
          </div>
          {msg && <p className="text-xs text-muted-foreground">{msg}</p>}
          {!data ? (
            <p className="text-xs text-muted-foreground">Loading…</p>
          ) : unmapped.length > 0 ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-muted-foreground border-b border-border">
                    <th className="py-1.5 pr-2">Symbol</th>
                    <th className="py-1.5">Assign class</th>
                  </tr>
                </thead>
                <tbody>
                  {unmapped.map((sym) => {
                    const draft = drafts[sym] ?? "";
                    return (
                      <tr
                        key={sym}
                        className="border-b border-border/40 align-middle"
                      >
                        <td className="py-1.5 pr-2 font-mono font-medium">
                          {sym}
                        </td>
                        <td className="py-1.5">
                          <div className="flex flex-wrap items-center gap-2">
                            <select
                              className="max-w-[16rem] rounded border border-border bg-background px-1 py-0.5"
                              disabled={busy}
                              value={draft}
                              onChange={(e) =>
                                setDrafts((prev) => ({
                                  ...prev,
                                  [sym]: e.target.value,
                                }))
                              }
                            >
                              <option value="">— pick class —</option>
                              {data.plan_classes.map((c) => (
                                <option key={c} value={c}>
                                  {c}
                                </option>
                              ))}
                            </select>
                            <button
                              type="button"
                              disabled={busy || !draft}
                              onClick={() => void reassign(sym, draft)}
                              className="rounded-md border border-border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide hover:bg-secondary/50 disabled:opacity-40"
                            >
                              Confirm
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground">
                No unassigned held symbols.
              </p>
              {!adding ? (
                <button
                  type="button"
                  disabled={busy || data.rows.length === 0}
                  onClick={() => setAdding(true)}
                  className="rounded-md border border-border px-2.5 py-1 text-xs font-medium hover:bg-secondary/50 disabled:opacity-50"
                  title={
                    data.rows.length === 0
                      ? "Seed the map first"
                      : "Reassign a mapped symbol"
                  }
                >
                  +
                </button>
              ) : (
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <select
                    className="rounded border border-border bg-background px-1 py-0.5 font-mono"
                    disabled={busy}
                    value={addSymbol}
                    onChange={(e) => {
                      setAddSymbol(e.target.value);
                      setAddClass("");
                    }}
                  >
                    <option value="">— symbol —</option>
                    {data.rows.map((r) => (
                      <option key={r.symbol} value={r.symbol}>
                        {r.symbol}
                      </option>
                    ))}
                  </select>
                  <select
                    className="max-w-[16rem] rounded border border-border bg-background px-1 py-0.5"
                    disabled={busy || !addSymbol}
                    value={addClass}
                    onChange={(e) => setAddClass(e.target.value)}
                  >
                    <option value="">— pick class —</option>
                    {data.plan_classes.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    disabled={busy || !addSymbol || !addClass}
                    onClick={() => void reassign(addSymbol, addClass)}
                    className="rounded-md border border-border px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide hover:bg-secondary/50 disabled:opacity-40"
                  >
                    Confirm
                  </button>
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-foreground"
                    onClick={() => {
                      setAdding(false);
                      setAddSymbol("");
                      setAddClass("");
                    }}
                  >
                    Cancel
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </CollapsibleSection>
  );
}

"use client";

import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";

import { api, type AnomalyCardDTO } from "@/lib/api";
import {
  transactionsApi,
  type TransactionOut,
} from "@/lib/expenses/api";
import { cn } from "@/lib/utils";

const USER_ID = "ariel";

type TabKey = "details" | "anomaly";

interface Props {
  tx: TransactionOut;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Sprint #2 commit #11 — initial tab selection. Inline anomaly
   *  badges in <TransactionsTable> open this dialog directly on the
   *  Anomaly tab; the legacy ⓘ button opens it on Details. */
  initialTab?: TabKey;
  /** Open anomaly rows for this transaction (pre-fetched by the parent).
   *  When non-empty, the Anomaly tab becomes selectable. */
  anomalies?: AnomalyCardDTO[];
  /** Called after a queue row is dismissed; the parent uses this to
   *  prune its local anomalyMap so the badge disappears immediately. */
  onAnomalyDismissed?: (queueId: number) => void;
}

function fmtValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

function severityBadgeClass(s: AnomalyCardDTO["severity"]): string {
  if (s === "critical") return "bg-rose-500/15 text-rose-500 border-rose-500/40";
  if (s === "warning") return "bg-warning/15 text-warning border-warning/40";
  return "bg-info/15 text-info border-info/40";
}

export function TransactionDetailsDialog({
  tx, open, onOpenChange,
  initialTab = "details",
  anomalies = [],
  onAnomalyDismissed,
}: Props) {
  const [opening, setOpening] = useState(false);
  const [openResult, setOpenResult] = useState<string | null>(null);
  // Tab state — initialize from the initialTab prop. We deliberately
  // DON'T sync via effect when initialTab changes mid-mount: the parent
  // unmounts/remounts the dialog when switching transactions (the
  // `detailsTx &&` conditional), so the initializer re-runs naturally.
  // Avoids the react-hooks/set-state-in-effect lint rule.
  const [tab, setTab] = useState<TabKey>(initialTab);
  // Optimistically-dismissed queue-row ids. We derive the visible
  // anomaly list from the parent's prop minus this set so a Dismiss
  // click removes the row immediately without a refetch + without
  // mirroring the prop into local state via an effect.
  const [dismissedIds, setDismissedIds] = useState<ReadonlySet<number>>(() => new Set());
  const [dismissingId, setDismissingId] = useState<number | null>(null);
  const [dismissError, setDismissError] = useState<string | null>(null);

  const visibleAnomalies = useMemo<AnomalyCardDTO[]>(
    () => anomalies.filter((c) => !dismissedIds.has(c.id)),
    [anomalies, dismissedIds],
  );

  async function openInApp() {
    setOpenResult(null);
    setOpening(true);
    try {
      const r = await transactionsApi.openSourceFile(tx.id, USER_ID);
      if (r.status === "ok") {
        setOpenResult(`Launched: ${r.storage_path}`);
      } else if (r.status === "missing") {
        setOpenResult(`File missing on disk: ${r.storage_path}`);
      } else {
        setOpenResult(`Could not open: ${r.message ?? r.status}`);
      }
    } catch (e) {
      setOpenResult(`Error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setOpening(false);
    }
  }

  async function dismissAnomaly(id: number) {
    setDismissError(null);
    setDismissingId(id);
    try {
      await api.anomalyDismiss(id, USER_ID);
      setDismissedIds((prev) => {
        const next = new Set(prev);
        next.add(id);
        return next;
      });
      onAnomalyDismissed?.(id);
    } catch (e) {
      setDismissError(e instanceof Error ? e.message : String(e));
    } finally {
      setDismissingId(null);
    }
  }

  const rawEntries = Object.entries(tx.raw_row ?? {});
  const anomalyCount = visibleAnomalies.length;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Transaction details · #{tx.id}</DialogTitle>
        </DialogHeader>

        {/* Tab strip — appears whenever there's an anomaly to show OR the
            caller opened on the Anomaly tab directly. */}
        {(anomalyCount > 0 || tab === "anomaly") && (
          <div className="flex gap-2 border-b border-border">
            <button
              type="button"
              onClick={() => setTab("details")}
              className={cn(
                "px-3 py-1 text-sm border-b-2 -mb-px",
                tab === "details"
                  ? "border-foreground text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              Details
            </button>
            <button
              type="button"
              onClick={() => setTab("anomaly")}
              className={cn(
                "px-3 py-1 text-sm border-b-2 -mb-px",
                tab === "anomaly"
                  ? "border-foreground text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              Anomaly{anomalyCount > 0 ? ` (${anomalyCount})` : ""}
            </button>
          </div>
        )}

        {tab === "details" && (
          <div className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
              <div className="text-muted-foreground">Occurred</div>
              <div>{tx.occurred_on}</div>
              <div className="text-muted-foreground">Merchant (raw)</div>
              <div className="font-mono break-all">{tx.merchant_raw}</div>
              <div className="text-muted-foreground">Amount</div>
              <div className="tabular-nums">
                {tx.amount_nis !== null
                  ? `₪${tx.amount_nis.toLocaleString()}`
                  : tx.amount_orig !== null && tx.currency_orig
                    ? `${tx.amount_orig.toLocaleString()} ${tx.currency_orig}`
                    : "—"}
                {" "}({tx.direction})
              </div>
              <div className="text-muted-foreground">Category</div>
              <div>
                {tx.category_slug ?? "uncategorized"}
                {tx.category_source && (
                  <span className="ml-2 text-xs text-muted-foreground">
                    src={tx.category_source}
                  </span>
                )}
              </div>
              <div className="text-muted-foreground">Statement / source</div>
              <div className="text-xs">
                statement #{tx.statement_id} · source #{tx.source_id}
              </div>
            </div>

            <div className="border-t border-border pt-3">
              <div className="text-xs text-muted-foreground mb-2">
                Original row from the statement file
              </div>
              {rawEntries.length === 0 ? (
                <div className="text-xs text-muted-foreground italic">
                  No raw row preserved.
                </div>
              ) : (
                <div className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 text-xs font-mono">
                  {rawEntries.map(([k, v]) => (
                    <div key={k} className="contents">
                      <div className="text-muted-foreground">{k}</div>
                      <div className="break-all whitespace-pre-wrap">{fmtValue(v)}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="border-t border-border pt-3 flex items-center justify-between">
              <div className="text-xs text-muted-foreground">
                Launches the original file in the OS default handler (Excel for .xls/.xlsx, browser for .html).
              </div>
              <Button onClick={openInApp} disabled={opening} size="sm">
                {opening ? "Opening…" : "Open file"}
              </Button>
            </div>
            {openResult && (
              <div className="text-xs text-muted-foreground break-all">
                {openResult}
              </div>
            )}
          </div>
        )}

        {tab === "anomaly" && (
          <div className="flex flex-col gap-3">
            {anomalyCount === 0 ? (
              <div className="text-sm text-success py-4 text-center">
                ✓ No open anomalies for this transaction.
              </div>
            ) : (
              <ul className="flex flex-col gap-2">
                {visibleAnomalies.map((c) => (
                  <li
                    key={c.id}
                    className="border border-border rounded p-2 flex flex-col gap-1"
                  >
                    <div className="flex items-center gap-2">
                      <span
                        className={cn(
                          "text-xs px-2 py-0.5 rounded border uppercase tracking-wide",
                          severityBadgeClass(c.severity),
                        )}
                      >
                        {c.severity}
                      </span>
                      <span className="text-xs text-muted-foreground">{c.kind}</span>
                    </div>
                    <div className="text-sm font-medium">{c.message}</div>
                    {c.detail && (
                      <div className="text-xs text-muted-foreground">{c.detail}</div>
                    )}
                    <div className="flex items-center justify-end gap-2 mt-1">
                      {c.link && (
                        <a
                          href={c.link}
                          className="text-xs text-muted-foreground hover:underline"
                        >
                          Open detail
                        </a>
                      )}
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={dismissingId === c.id}
                        onClick={() => dismissAnomaly(c.id)}
                      >
                        {dismissingId === c.id ? "Dismissing…" : "Dismiss"}
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
            {dismissError && (
              <div className="text-xs text-rose-500 break-all">
                {dismissError}
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

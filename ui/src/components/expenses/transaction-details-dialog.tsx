"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";

import {
  transactionsApi,
  type TransactionOut,
} from "@/lib/expenses/api";

const USER_ID = "ariel";

interface Props {
  tx: TransactionOut;
  open: boolean;
  onOpenChange: (open: boolean) => void;
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

export function TransactionDetailsDialog({ tx, open, onOpenChange }: Props) {
  const [opening, setOpening] = useState(false);
  const [openResult, setOpenResult] = useState<string | null>(null);

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

  const rawEntries = Object.entries(tx.raw_row ?? {});

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Transaction details · #{tx.id}</DialogTitle>
        </DialogHeader>

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
      </DialogContent>
    </Dialog>
  );
}

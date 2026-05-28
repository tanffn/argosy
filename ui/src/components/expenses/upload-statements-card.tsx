"use client";

import { useCallback, useRef, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import {
  expensesApi,
  type UploadFileResult,
  type UploadStatementsResponse,
} from "@/lib/expenses/api";

interface Props {
  userId: string;
  /** Called after a successful (or partially-successful) upload so the
   *  parent can refresh dashboards. Skipped on total-failure responses. */
  onUploadComplete?: (resp: UploadStatementsResponse) => void;
}

/**
 * Drag-drop bank-statement ingest tile.
 *
 * Routes through `POST /api/expenses/upload`, which is the canonical
 * `catalog_upload` funnel per SDD §17.1 — same backend path the
 * `update_leumi_tsv.py` workflow uses. Multi-file; each file's outcome
 * is reported inline so the user sees which parsed and which failed
 * without leaving the page.
 *
 * Max issuer needs a `card_last4` hint (the statement itself only
 * carries the bank account it bills to, not the card last-4); we
 * surface a small input that ONLY shows once a Max-shaped file is
 * detected by the backend's failure response. For all other issuers
 * (Leumi, Schwab, Discount, Cal) the hint stays hidden.
 */
export function UploadStatementsCard({ userId, onUploadComplete }: Props) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<UploadFileResult[] | null>(null);
  const [cardLast4, setCardLast4] = useState<string>("");
  // Show the card-last4 input once a Max failure surfaces. The user can
  // re-try after typing the hint; we don't surface this input upfront
  // because most uploads aren't Max statements.
  const [needsLast4, setNeedsLast4] = useState(false);

  const uploadFiles = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      setBusy(true);
      setError(null);
      setResults(null);
      try {
        const resp = await expensesApi.uploadStatements(
          userId,
          files,
          cardLast4 || undefined,
        );
        setResults(resp.results);
        const maxNeedsLast4 = resp.results.some(
          (r) =>
            r.status === "failed" &&
            (r.error ?? "").toLowerCase().includes("card_last4"),
        );
        setNeedsLast4(maxNeedsLast4);
        onUploadComplete?.(resp);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [userId, cardLast4, onUploadComplete],
  );

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragActive(false);
      const files = Array.from(e.dataTransfer.files);
      void uploadFiles(files);
    },
    [uploadFiles],
  );

  const onPick = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files ?? []);
      // Reset the input so re-picking the same file re-triggers onChange.
      if (e.target.value) e.target.value = "";
      void uploadFiles(files);
    },
    [uploadFiles],
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-base font-mono">
            Upload bank statements
          </CardTitle>
          <span className="text-[11px] text-muted-foreground">
            Leumi &middot; Schwab &middot; Discount &middot; Cal &middot; Max
          </span>
        </div>
      </CardHeader>
      <CardContent>
        <div
          role="button"
          tabIndex={0}
          onClick={() => inputRef.current?.click()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") inputRef.current?.click();
          }}
          onDragOver={(e) => {
            e.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={() => setDragActive(false)}
          onDrop={onDrop}
          className={`rounded-md border-2 border-dashed px-4 py-6 text-center cursor-pointer transition-colors ${
            dragActive
              ? "border-info bg-info/5"
              : "border-border bg-secondary/30 hover:border-muted-foreground/50"
          }`}
          aria-label="Drop bank statement files here, or click to pick"
        >
          <input
            ref={inputRef}
            type="file"
            multiple
            accept=".pdf,.csv,.xls,.xlsx,.tsv,application/pdf,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            className="hidden"
            onChange={onPick}
            disabled={busy}
          />
          <div className="font-mono text-sm">
            {busy ? (
              <>Uploading &amp; parsing&hellip;</>
            ) : dragActive ? (
              <>Drop to ingest</>
            ) : (
              <>
                Drop a bank statement here, or <span className="text-info underline-offset-2 hover:underline">click to pick</span>
              </>
            )}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            Routes through <code className="font-mono">catalog_upload</code>{" "}
            (SDD &sect;17.1). PDF / CSV / XLS / XLSX / TSV supported.
            Multi-file OK. Per-file outcome reported below.
          </div>
        </div>

        {needsLast4 ? (
          <div className="mt-3 flex items-center gap-2 flex-wrap">
            <span className="font-mono text-[11px] text-amber-400">
              Max statement detected &mdash; card last 4 required:
            </span>
            <input
              type="text"
              maxLength={4}
              pattern="\d{4}"
              value={cardLast4}
              onChange={(e) =>
                setCardLast4(e.target.value.replace(/\D/g, "").slice(0, 4))
              }
              className="w-20 rounded border border-border bg-background/60 px-2 py-1 font-mono text-sm tabular-nums focus:outline-none focus:ring-1 focus:ring-info/50"
              placeholder="1234"
              aria-label="Max card last 4 digits"
            />
            <span className="font-mono text-[11px] text-muted-foreground">
              then re-upload.
            </span>
          </div>
        ) : null}

        {error ? (
          <div className="mt-3 text-sm text-rose-400 font-mono">
            Upload failed: {error}
          </div>
        ) : null}

        {results && results.length > 0 ? (
          <div className="mt-4 space-y-1.5">
            <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
              Results
            </div>
            <ul className="space-y-1.5">
              {results.map((r, i) => (
                <li
                  key={`${r.filename}-${i}`}
                  className="rounded border border-border/40 bg-secondary/20 px-3 py-2"
                >
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <span className="font-mono text-sm truncate min-w-0">
                      {r.filename}
                    </span>
                    <StatusPill
                      tone={r.status === "parsed" ? "success" : "error"}
                      mono
                    >
                      {r.status.toUpperCase()}
                    </StatusPill>
                  </div>
                  {r.status === "parsed" ? (
                    <div className="mt-1 font-mono text-[11px] text-muted-foreground tabular-nums">
                      parser: {r.parser_name ?? "&mdash;"} &middot;{" "}
                      {r.transactions_inserted} tx &middot;{" "}
                      {r.categories_resolved} categorised &middot;{" "}
                      {r.correlations_made} correlated &middot;{" "}
                      {r.refunds_matched} refunds
                    </div>
                  ) : (
                    <div className="mt-1 font-mono text-[11px] text-rose-400">
                      {r.error ?? "unknown error"}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

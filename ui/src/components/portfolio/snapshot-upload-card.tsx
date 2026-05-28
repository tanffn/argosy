"use client";

import Link from "next/link";
import { useCallback, useRef, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type PortfolioUploadSnapshotResponse,
} from "@/lib/api";

interface Props {
  userId: string;
  onUploadComplete?: (resp: PortfolioUploadSnapshotResponse) => void;
}

/**
 * Monthly portfolio-snapshot upload tile (2026-05-29).
 *
 * User mental model (per the 2026-05-28 reframe): every month you drop
 * bank statements (transactions) into <span class="nav-card">/expenses</span>
 * AND a portfolio snapshot (positions) into <span class="nav-card">/portfolio</span>.
 * This tile is the latter half — it accepts the
 * "Family Finances Status - YY MMM.tsv" file the user already produces
 * monthly, persists it under the windfall-detector scan root, and fires
 * the detector synchronously so a qualifying cash delta surfaces
 * inline.
 *
 * Scope note: the raw XLS-to-TSV conversion is still the user's
 * external `update_leumi_tsv.py` script. The in-repo Leumi adapter
 * (`argosy/adapters/brokers/leumi_tsv.py`) only reads an existing TSV;
 * no XLS parser ships in-repo yet. Once one does, this tile can extend
 * to accept .xls directly.
 */
export function PortfolioSnapshotUploadCard({ userId, onUploadComplete }: Props) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PortfolioUploadSnapshotResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const uploadFile = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      // One file at a time -- a monthly snapshot is a single TSV; users
      // shouldn't be batch-uploading 12 of them. If they want a historical
      // backfill, that's a CLI job.
      const file = files[0];
      setBusy(true);
      setError(null);
      setResult(null);
      try {
        const resp = await api.portfolioUploadSnapshot(userId, file, true);
        setResult(resp);
        onUploadComplete?.(resp);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [userId, onUploadComplete],
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <CardTitle className="text-base font-mono">
            Upload monthly portfolio snapshot
          </CardTitle>
          <span className="text-[11px] text-muted-foreground">
            Leumi &middot; Schwab &middot; Aborad &middot; combined TSV
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
          onDrop={(e) => {
            e.preventDefault();
            setDragActive(false);
            void uploadFile(Array.from(e.dataTransfer.files));
          }}
          className={`rounded-md border-2 border-dashed px-4 py-5 text-center cursor-pointer transition-colors ${
            dragActive
              ? "border-info bg-info/5"
              : "border-border bg-secondary/30 hover:border-muted-foreground/50"
          }`}
          aria-label="Drop a monthly portfolio TSV here, or click to pick"
        >
          <input
            ref={inputRef}
            type="file"
            accept=".tsv,text/tab-separated-values"
            className="hidden"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              if (e.target.value) e.target.value = "";
              void uploadFile(files);
            }}
            disabled={busy}
          />
          <div className="font-mono text-sm">
            {busy ? (
              <>Uploading + running windfall detector&hellip;</>
            ) : dragActive ? (
              <>Drop to ingest</>
            ) : (
              <>
                Drop the monthly &quot;Family Finances Status&quot; TSV here,{" "}
                or <span className="text-info underline-offset-2 hover:underline">click to pick</span>
              </>
            )}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            The TSV gets persisted under{" "}
            <code className="font-mono">ARGOSY_EXPENSE_SAMPLES_ROOT</code>;
            the windfall detector diffs it against the previous month
            inline. Raw <code className="font-mono">.xls</code> upload is
            on the roadmap.
          </div>
        </div>

        {error ? (
          <div className="mt-3 text-sm text-rose-400 font-mono">
            Upload failed: {error}
          </div>
        ) : null}

        {result ? <UploadResultRow result={result} /> : null}
      </CardContent>
    </Card>
  );
}

function UploadResultRow({ result }: { result: PortfolioUploadSnapshotResponse }) {
  if (!result.tsv_persisted) {
    return (
      <div className="mt-3 rounded-md border border-rose-400/40 bg-rose-400/5 px-3 py-2 font-mono text-xs text-rose-400">
        <div className="font-semibold mb-1">Snapshot rejected</div>
        <div>{result.detail ?? "unknown error"}</div>
      </div>
    );
  }
  return (
    <div className="mt-3 space-y-2">
      <div className="rounded-md border border-emerald-400/30 bg-emerald-400/5 px-3 py-2 font-mono text-xs">
        <div className="flex items-center gap-2 flex-wrap">
          <StatusPill tone="success" mono>
            PERSISTED
          </StatusPill>
          <span className="text-emerald-400">
            Snapshot {result.snapshot_date ?? "—"}
          </span>
          <span className="text-muted-foreground">
            sha {result.sha256.slice(0, 8)}
          </span>
        </div>
        <div className="mt-1 text-[11px] text-muted-foreground truncate">
          {result.persisted_path}
        </div>
      </div>

      {result.detect_status === "skipped" ? (
        <div className="rounded-md border border-border/50 bg-secondary/30 px-3 py-2 font-mono text-xs text-muted-foreground">
          <span className="font-semibold text-foreground">Detector skipped</span> &mdash;
          first snapshot on file (nothing to diff against yet). Next month&apos;s
          upload will fire the detector.
        </div>
      ) : null}

      {result.detect_status === "failed" ? (
        <div className="rounded-md border border-rose-400/30 bg-rose-400/5 px-3 py-2 font-mono text-xs text-rose-400">
          <span className="font-semibold">Detector failed</span> &mdash; the
          TSV is persisted; the windfall check raised. Check the server
          log under <code>portfolio_snapshot.detector_failed</code> for
          the cause.
        </div>
      ) : null}

      {result.detect_status === "ok" && result.event === null ? (
        <div className="rounded-md border border-border/50 bg-secondary/30 px-3 py-2 font-mono text-xs text-muted-foreground">
          <span className="font-semibold text-foreground">No windfall</span> &mdash;
          cash delta below threshold ($25K USD / ₪75K NIS).
        </div>
      ) : null}

      {result.detect_status === "ok" && result.event !== null ? (
        <div className="rounded-md border border-warning/40 bg-warning/5 px-3 py-2 font-mono text-xs">
          <div className="flex items-center gap-2 flex-wrap">
            <span aria-hidden className="text-warning">⚠</span>
            <span className="font-semibold">
              ${Math.round(result.event.cash_delta_total_usd_equiv).toLocaleString()} windfall detected
            </span>
            <StatusPill
              tone={result.event.requires_user_classification ? "warning" : "accent"}
              mono
            >
              {result.event.classified_source.replace("_", " ").toUpperCase()}
            </StatusPill>
          </div>
          <div className="mt-2">
            <Link
              href="/retirement#windfall"
              className="text-info hover:underline"
            >
              See allocation plan -&gt;
            </Link>
          </div>
        </div>
      ) : null}
    </div>
  );
}

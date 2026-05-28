"use client";

/* eslint-disable react-hooks/set-state-in-effect --
 * Fetch-on-mount + filter-reset pattern. The first effect (refresh) pulls
 * the file list and sets local state; the second resets the pagination
 * offset whenever filters change. Planned Suspense / React Query migration
 * will dissolve this — see SDD "fetch-on-mount" note.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  FileText,
  Image as ImageIcon,
  FileSpreadsheet,
  FileCode2,
  File as FileIcon,
  Upload,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type UserFileItem } from "@/lib/api";

const USER_ID = "ariel";
const PAGE_SIZE = 50;

const KIND_OPTIONS = [
  { value: "", label: "Any" },
  { value: "plan_markdown", label: "Plan markdown" },
  { value: "image", label: "Image" },
  { value: "text", label: "Text" },
  { value: "broker_csv", label: "Broker CSV" },
  { value: "other", label: "Other" },
];

const SOURCE_OPTIONS = [
  { value: "", label: "Any" },
  { value: "chat_attachment", label: "Chat attachment" },
  { value: "intake_upload", label: "Intake plan upload" },
  { value: "intake_file_to_text", label: "Intake file conversion" },
  { value: "cost_basis_import", label: "Cost-basis CSV" },
  { value: "manual_upload", label: "Manual upload (Files tile)" },
  { value: "expense_statement", label: "Expense statement" },
];

const UPLOAD_KINDS = [
  { value: "other", label: "Other (default)" },
  { value: "text", label: "Text" },
  { value: "image", label: "Image" },
  { value: "plan_markdown", label: "Plan markdown" },
  { value: "broker_csv", label: "Broker CSV" },
];

function KindIcon({ kind }: { kind: string }) {
  const cls = "h-4 w-4 text-muted-foreground";
  switch (kind) {
    case "image":
      return <ImageIcon className={cls} aria-hidden suppressHydrationWarning />;
    case "plan_markdown":
      return <FileCode2 className={cls} aria-hidden suppressHydrationWarning />;
    case "broker_csv":
      return <FileSpreadsheet className={cls} aria-hidden suppressHydrationWarning />;
    case "text":
      return <FileText className={cls} aria-hidden suppressHydrationWarning />;
    default:
      return <FileIcon className={cls} aria-hidden suppressHydrationWarning />;
  }
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTimestamp(iso: string): string {
  // Show ISO date + 24h time, no seconds.
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const date = d.toISOString().slice(0, 10);
  const time = d.toISOString().slice(11, 16);
  return `${date} ${time}Z`;
}

export default function FilesPage() {
  const [items, setItems] = useState<UserFileItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState<string>("");
  const [source, setSource] = useState<string>("");
  const [includeDeleted, setIncludeDeleted] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const r = await api.listFiles(USER_ID, {
        kind: kind || undefined,
        source: source || undefined,
        includeDeleted: includeDeleted || undefined,
        limit: PAGE_SIZE,
        offset,
      });
      setItems(r.items);
      setTotal(r.total);
      setError(null);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [kind, source, includeDeleted, offset]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Reset to first page whenever filters change.
  useEffect(() => {
    setOffset(0);
  }, [kind, source, includeDeleted]);

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Files</h1>
        <p className="text-sm text-muted-foreground">
          Every file you&apos;ve uploaded to Argosy — chat attachments,
          plan imports, broker CSVs — cataloged with date, kind, and a link
          back to the decision or plan that consumed it.
        </p>
      </header>

      <UploadTile onUploaded={refresh} />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Filters</CardTitle>
          <CardDescription>
            Empty fields = no filter.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-2 md:grid-cols-4 gap-3 items-end">
          <label className="flex flex-col text-xs gap-1">
            <span className="text-muted-foreground">Kind</span>
            <select
              className="bg-background border border-border rounded-md px-3 py-1.5 text-sm font-mono"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
            >
              {KIND_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col text-xs gap-1">
            <span className="text-muted-foreground">Source</span>
            <select
              className="bg-background border border-border rounded-md px-3 py-1.5 text-sm font-mono"
              value={source}
              onChange={(e) => setSource(e.target.value)}
            >
              {SOURCE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
          <label className="inline-flex items-center gap-2 text-xs col-span-2 md:col-span-1">
            <input
              type="checkbox"
              checked={includeDeleted}
              onChange={(e) => setIncludeDeleted(e.target.checked)}
            />
            <span className="text-muted-foreground">
              Include deleted
            </span>
          </label>
          <div className="col-span-2 md:col-span-1 flex justify-end">
            <Button size="sm" onClick={refresh}>
              Refresh
            </Button>
          </div>
        </CardContent>
      </Card>

      {error && <p className="text-sm text-error font-mono">{error}</p>}
      {loading && <p className="text-sm text-muted-foreground">Loading...</p>}

      {!loading && items.length === 0 && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No files match. Upload a file via the Advisor chat or run the
            backfill CLI to populate the catalog.
          </CardContent>
        </Card>
      )}

      {!loading && items.length > 0 && (
        <Card>
          <CardContent className="p-0">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="text-left py-2 px-3"></th>
                  <th className="text-left py-2 px-3">name</th>
                  <th className="text-left py-2 px-3">kind</th>
                  <th className="text-left py-2 px-3">source</th>
                  <th className="text-right py-2 px-3">size</th>
                  <th className="text-left py-2 px-3">when</th>
                  <th className="text-left py-2 px-3">links</th>
                  <th className="text-right py-2 px-3"></th>
                </tr>
              </thead>
              <tbody>
                {items.map((f) => (
                  <tr
                    key={f.id}
                    className={
                      "border-b border-border/40 align-top " +
                      (f.deleted_at ? "opacity-50" : "")
                    }
                  >
                    <td className="py-2 px-3">
                      <KindIcon kind={f.kind} />
                    </td>
                    <td className="py-2 px-3 break-all max-w-[20rem]">
                      <span title={f.sanitized_name}>{f.original_name}</span>
                      {f.deleted_at && (
                        <span className="ml-2 text-[10px] text-error">
                          (deleted)
                        </span>
                      )}
                    </td>
                    <td className="py-2 px-3 whitespace-nowrap">{f.kind}</td>
                    <td className="py-2 px-3 whitespace-nowrap">{f.source}</td>
                    <td className="py-2 px-3 whitespace-nowrap text-right">
                      {formatSize(f.size_bytes)}
                    </td>
                    <td className="py-2 px-3 whitespace-nowrap">
                      {formatTimestamp(f.created_at)}
                    </td>
                    <td className="py-2 px-3 whitespace-nowrap">
                      {f.plan_version_id !== null && (
                        <a
                          href={`/plan`}
                          className="text-primary hover:underline mr-2"
                        >
                          plan
                        </a>
                      )}
                      {f.decision_run_id !== null && (
                        <a
                          href={`/decisions/${f.decision_run_id}`}
                          className="text-primary hover:underline"
                        >
                          decision #{f.decision_run_id}
                        </a>
                      )}
                    </td>
                    <td className="py-2 px-3 whitespace-nowrap text-right">
                      <a
                        href={api.fileContentUrl(f.id, USER_ID)}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-primary hover:underline"
                      >
                        open
                      </a>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      {/* Pagination */}
      {!loading && total > PAGE_SIZE && (
        <div className="flex items-center justify-between text-xs font-mono text-muted-foreground">
          <span>
            Showing {offset + 1}–{Math.min(offset + items.length, total)} of {total}
          </span>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="secondary"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Prev
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </main>
  );
}

interface UploadTileProps {
  onUploaded: () => void;
}

function UploadTile({ onUploaded }: UploadTileProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragActive, setDragActive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState<string>("other");
  const [lastUploaded, setLastUploaded] = useState<UserFileItem | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const uploadFiles = useCallback(
    async (files: File[]) => {
      if (files.length === 0) return;
      setBusy(true);
      setUploadError(null);
      try {
        // One-at-a-time so each catalog row gets a clean per-file
        // record. Multi-file batch wasn't worth the API surface for
        // the manual-upload use case.
        let last: UserFileItem | null = null;
        for (const f of files) {
          last = await api.uploadFile("ariel", f, kind);
        }
        setLastUploaded(last);
        onUploaded();
      } catch (e) {
        setUploadError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [kind, onUploaded],
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <CardTitle className="text-base flex items-center gap-2">
            <Upload className="h-4 w-4" aria-hidden suppressHydrationWarning />
            Upload to catalog
          </CardTitle>
          <label className="flex items-center gap-2 text-xs">
            <span className="text-muted-foreground">kind:</span>
            <select
              className="bg-background border border-border rounded-md px-2 py-1 text-xs font-mono"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              disabled={busy}
            >
              {UPLOAD_KINDS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          </label>
        </div>
        <CardDescription>
          Drop a file or click to pick. Routes through{" "}
          <code className="font-mono">catalog_upload</code> (SDD &sect;17.1) —
          same backend as the Advisor chat Attach button. Cataloged here,
          downstream agents (intake / plan-distill / categorizer) can pick
          it up depending on kind.
        </CardDescription>
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
            void uploadFiles(Array.from(e.dataTransfer.files));
          }}
          className={`rounded-md border-2 border-dashed px-4 py-5 text-center cursor-pointer transition-colors ${
            dragActive
              ? "border-info bg-info/5"
              : "border-border bg-secondary/30 hover:border-muted-foreground/50"
          }`}
          aria-label="Drop file here, or click to pick"
        >
          <input
            ref={inputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              if (e.target.value) e.target.value = "";
              void uploadFiles(files);
            }}
            disabled={busy}
          />
          <div className="font-mono text-sm">
            {busy ? (
              <>Uploading&hellip;</>
            ) : dragActive ? (
              <>Drop to upload</>
            ) : (
              <>
                Drop a file, or{" "}
                <span className="text-info underline-offset-2 hover:underline">
                  click to pick
                </span>
              </>
            )}
          </div>
        </div>
        {uploadError ? (
          <div className="mt-3 text-sm text-rose-400 font-mono">
            Upload failed: {uploadError}
          </div>
        ) : null}
        {lastUploaded ? (
          <div className="mt-3 text-[11px] font-mono text-emerald-400">
            ✓ Uploaded <span className="font-semibold">{lastUploaded.original_name}</span>
            {" "}({lastUploaded.kind}, {formatSize(lastUploaded.size_bytes)})
            — catalog row #{lastUploaded.id}.
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

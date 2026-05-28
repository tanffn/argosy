"use client";

import { useEffect, useState } from "react";
import { api, type Source, type SourcesResponse } from "@/lib/api";

interface Props {
  /**
   * If null, render the full canonical registry. Otherwise filter to only
   * the IDs in the array (typically: the ids referenced by ValueWithTooltip
   * components on the same page).
   */
  filterIds: string[] | null;
}

const KIND_BADGE: Record<Source["kind"], string> = {
  official: "bg-emerald-500/20 text-emerald-300",
  research: "bg-sky-500/20 text-sky-300",
  best_effort: "bg-amber-500/20 text-amber-300",
  derived: "bg-slate-500/20 text-slate-300",
};

/**
 * Sources panel — bottom-of-page citation list. Renders the canonical
 * registry filtered to ids actually used on the current page.
 *
 * Each entry gets an anchor id (`src-<id>`) so ValueWithTooltip popovers
 * can link directly to the relevant entry via href="#src-<id>".
 */
export function SourcesPanel({ filterIds }: Props) {
  const [data, setData] = useState<SourcesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.retirement
      .sources()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <p className="text-sm text-rose-400">
        Failed to load sources: {error}
      </p>
    );
  }
  if (!data) {
    return <p className="text-sm text-muted-foreground">Loading sources…</p>;
  }

  const ids = filterIds ?? Object.keys(data.sources);
  const visible = ids
    .map((id) => data.sources[id])
    .filter((s): s is Source => Boolean(s));

  if (visible.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No sources referenced on this page yet.
      </p>
    );
  }

  return (
    <ol className="space-y-2 text-sm">
      {visible.map((s, i) => (
        <li
          key={s.id}
          id={`src-${s.id}`}
          className="rounded-md border border-border/40 bg-background/40 px-3 py-2"
        >
          <div className="flex items-baseline gap-2 flex-wrap">
            <span className="font-mono text-xs text-muted-foreground">
              [{i + 1}]
            </span>
            <span className="font-medium">{s.title}</span>
            <span
              className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${
                KIND_BADGE[s.kind]
              }`}
            >
              {s.kind}
            </span>
            {s.as_of && (
              <span className="text-[10px] font-mono text-muted-foreground">
                as of {s.as_of}
              </span>
            )}
          </div>
          {s.url && (
            <a
              href={s.url}
              target="_blank"
              rel="noopener noreferrer"
              className="block mt-0.5 text-xs text-sky-400 hover:underline truncate"
            >
              {s.url}
            </a>
          )}
          {s.notes && (
            <p className="mt-1 text-xs text-muted-foreground">{s.notes}</p>
          )}
        </li>
      ))}
    </ol>
  );
}

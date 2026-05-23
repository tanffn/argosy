"use client";

/**
 * AgentDetailDrawer — tabbed side-drawer for a single agent run row.
 *
 * Tabs (in order):
 *   1. Prompt     — full system + user prompt fetched on-demand from
 *                   /api/agent-activity/{id}/prompt (Wave B-UI follow-up #2).
 *                   Skipped (replaced with "Awaiting persistence") when
 *                   row.id === -1 (WS-only, not yet in DB).
 *   2. Output     — response_text rendered via <Markdown>
 *   3. Sources    — <source id="...">...</source> blocks parsed from the
 *                   prompt (not yet exposed by backend; Wave B-UI Task 9).
 *   4. Citations  — parsed citations_json list
 *   5. Cost & telemetry — token/cost table
 *
 * Uses the minimal Sheet + Tabs primitives already in the codebase
 * (no Radix dependency).
 */

import * as React from "react";

import { Markdown } from "@/components/markdown";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { api, type AgentActivityRow, type AgentPrompt } from "@/lib/api";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const USER_ID = "ariel";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type AgentDetailDrawerProps = {
  row: AgentActivityRow | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

interface CitationEntry {
  source_id: string;
  claim_text: string;
  cited_quote: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function parseCitations(raw: string | null | undefined): CitationEntry[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (Array.isArray(parsed)) return parsed as CitationEntry[];
  } catch {
    // malformed JSON — treat as empty
  }
  return [];
}

function formatCost(usd: number): string {
  if (usd === 0) return "$0.000000";
  return `$${usd.toFixed(6)}`;
}

function cacheHitRatio(cacheIn: number, tokensIn: number): string {
  if (tokensIn === 0) return "—";
  return `${((cacheIn / tokensIn) * 100).toFixed(1)}%`;
}

// ---------------------------------------------------------------------------
// Tab: Prompt
// ---------------------------------------------------------------------------

/**
 * PromptTab fetches the full system + user prompt on first render (when the
 * tab is selected), then caches by row.id so reopening the same row doesn't
 * refetch.
 *
 * Cache lives in a module-level Map so it persists across open/close cycles
 * of the drawer (but is cleared on full page reload, which is fine).
 */
const _promptCache = new Map<number, AgentPrompt>();

function PromptTab({ row }: { row: AgentActivityRow }) {
  // WS-only rows (id === -1) are not in the DB yet — show a placeholder.
  if (row.id === -1) {
    return (
      <p className="text-sm text-muted-foreground italic">
        Awaiting persistence — prompt will be available once the run is saved
        to the database.
      </p>
    );
  }

  const [prompt, setPrompt] = React.useState<AgentPrompt | null>(
    _promptCache.get(row.id) ?? null,
  );
  const [loading, setLoading] = React.useState<boolean>(false);

  React.useEffect(() => {
    // Already fetched for this row — nothing to do.
    if (prompt !== null) return;
    let cancelled = false;
    setLoading(true);
    api
      .agentActivityPrompt(row.id, USER_ID)
      .then((data) => {
        if (cancelled) return;
        _promptCache.set(row.id, data);
        setPrompt(data);
      })
      .catch(() => {
        if (cancelled) return;
        // On error, store an empty sentinel so we don't retry in a loop.
        const sentinel: AgentPrompt = { id: row.id, system_prompt: "", user_prompt: "" };
        _promptCache.set(row.id, sentinel);
        setPrompt(sentinel);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [row.id, prompt]);

  if (loading) {
    return (
      <p className="text-sm text-muted-foreground italic">
        Loading prompt&hellip;
      </p>
    );
  }

  if (!prompt || (prompt.system_prompt === "" && prompt.user_prompt === "")) {
    return (
      <p className="text-sm text-muted-foreground italic">
        Prompt not captured (row persisted before migration 0029).
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      {/* User prompt — default OPEN since it's the more debugging-relevant part */}
      <details open>
        <summary className="cursor-pointer select-none text-sm font-semibold py-1">
          User prompt
        </summary>
        <pre className="mt-2 max-h-96 overflow-y-auto whitespace-pre-wrap break-words text-xs font-mono bg-muted/40 rounded p-3 border border-border">
          {prompt.user_prompt}
        </pre>
      </details>

      {/* System prompt — default CLOSED; often large boilerplate */}
      <details>
        <summary className="cursor-pointer select-none text-sm font-semibold py-1">
          System prompt
        </summary>
        <pre className="mt-2 max-h-96 overflow-y-auto whitespace-pre-wrap break-words text-xs font-mono bg-muted/40 rounded p-3 border border-border">
          {prompt.system_prompt}
        </pre>
      </details>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tab: Output
// ---------------------------------------------------------------------------

function OutputTab({ row }: { row: AgentActivityRow }) {
  if (!row.response_text) {
    return (
      <p className="text-sm text-muted-foreground italic">
        No response text recorded for this run.
      </p>
    );
  }
  return <Markdown>{row.response_text}</Markdown>;
}

// ---------------------------------------------------------------------------
// Tab: Sources
// ---------------------------------------------------------------------------

interface SourcePreview {
  source_id: string;
  body_chars: number;
  body_head: string;
}

function SourcesTab({ row }: { row: AgentActivityRow }) {
  const previews: SourcePreview[] = row.sources_preview ?? [];

  if (previews.length === 0) {
    return (
      <p className="text-sm text-muted-foreground italic">
        No sources recorded for this run.
      </p>
    );
  }

  return (
    <ul className="flex flex-col gap-3">
      {previews.map((src, i) => (
        <li
          key={i}
          className="rounded-md border border-border bg-muted/30 p-3 flex flex-col gap-1"
        >
          {/* Source ID as title */}
          <p className="text-sm font-semibold font-mono leading-snug break-all">
            {src.source_id}
          </p>
          {/* Body head (truncated content preview) */}
          <p className="text-sm text-muted-foreground whitespace-pre-wrap break-words">
            {src.body_head}
            {src.body_chars > src.body_head.length && (
              <span className="text-xs italic ml-1">
                ... ({src.body_chars.toLocaleString()} total chars)
              </span>
            )}
          </p>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Tab: Citations
// ---------------------------------------------------------------------------

function CitationsTab({ row }: { row: AgentActivityRow }) {
  const citations = parseCitations(row.citations_json);

  if (citations.length === 0) {
    return (
      <p className="text-sm text-muted-foreground italic">
        No citations recorded for this run.
      </p>
    );
  }

  return (
    <ul className="flex flex-col gap-3">
      {citations.map((c, i) => (
        <li
          key={i}
          className="rounded-md border border-border bg-muted/30 p-3 flex flex-col gap-1"
        >
          {/* Claim */}
          <p className="text-sm font-medium leading-snug">{c.claim_text}</p>
          {/* Cited quote */}
          {c.cited_quote && (
            <blockquote className="border-l-2 border-primary/40 pl-3 text-sm text-muted-foreground italic">
              {c.cited_quote}
            </blockquote>
          )}
          {/* Source label */}
          <span className="text-xs text-muted-foreground">
            Source: {c.source_id}
          </span>
        </li>
      ))}
    </ul>
  );
}

// ---------------------------------------------------------------------------
// Tab: Cost & telemetry
// ---------------------------------------------------------------------------

function CostTab({ row }: { row: AgentActivityRow }) {
  const rows: [string, string][] = [
    ["Model", row.model],
    ["Tokens in", row.tokens_in.toLocaleString()],
    ["Tokens out", row.tokens_out.toLocaleString()],
    [
      "Cache read tokens",
      row.cache_input_tokens ? row.cache_input_tokens.toLocaleString() : "0",
    ],
    [
      "Cache write tokens",
      row.cache_creation_tokens
        ? row.cache_creation_tokens.toLocaleString()
        : "0",
    ],
    [
      "Thinking tokens",
      row.thinking_tokens ? row.thinking_tokens.toLocaleString() : "0",
    ],
    ["Cost (USD)", formatCost(row.cost_usd)],
    [
      "Cache hit ratio",
      cacheHitRatio(row.cache_input_tokens, row.tokens_in),
    ],
    ["Prompt hash", row.prompt_hash || "—"],
    ["Created at", new Date(row.created_at).toLocaleString()],
  ];

  return (
    <table className="w-full text-sm border-separate border-spacing-y-0.5">
      <tbody>
        {rows.map(([label, value]) => (
          <tr key={label}>
            <td className="w-1/2 pr-4 py-1 text-muted-foreground font-medium align-top">
              {label}
            </td>
            <td className="py-1 text-foreground font-mono break-all align-top">
              {value}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------
// Drawer root
// ---------------------------------------------------------------------------

export function AgentDetailDrawer({
  row,
  open,
  onOpenChange,
}: AgentDetailDrawerProps) {
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent className="w-full sm:max-w-[40vw] overflow-y-auto">
        {row ? (
          <>
            <SheetHeader>
              <SheetTitle>{row.agent_role}</SheetTitle>
              <SheetDescription>
                {row.model} &middot;{" "}
                {new Date(row.created_at).toLocaleString()}
                {row.decision_id ? ` · decision ${row.decision_id}` : ""}
              </SheetDescription>
            </SheetHeader>

            <Tabs defaultValue="prompt" className="flex-1 overflow-hidden">
              <TabsList className="w-full justify-start">
                <TabsTrigger value="prompt">Prompt</TabsTrigger>
                <TabsTrigger value="output">Output</TabsTrigger>
                <TabsTrigger value="sources">Sources</TabsTrigger>
                <TabsTrigger value="citations">
                  Citations
                  {row.citations_count > 0 && (
                    <span className="ml-1 rounded-full bg-primary/20 px-1.5 py-0.5 text-xs font-semibold leading-none">
                      {row.citations_count}
                    </span>
                  )}
                </TabsTrigger>
                <TabsTrigger value="cost">Cost &amp; telemetry</TabsTrigger>
              </TabsList>

              <TabsContent value="prompt" className="overflow-y-auto">
                <PromptTab row={row} />
              </TabsContent>

              <TabsContent value="output" className="overflow-y-auto">
                <OutputTab row={row} />
              </TabsContent>

              <TabsContent value="sources" className="overflow-y-auto">
                <SourcesTab row={row} />
              </TabsContent>

              <TabsContent value="citations" className="overflow-y-auto">
                <CitationsTab row={row} />
              </TabsContent>

              <TabsContent value="cost">
                <CostTab row={row} />
              </TabsContent>
            </Tabs>
          </>
        ) : (
          <div className="flex items-center justify-center h-full">
            <p className="text-sm text-muted-foreground">No run selected.</p>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}

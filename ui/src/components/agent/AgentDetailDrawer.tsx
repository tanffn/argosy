"use client";

/**
 * AgentDetailDrawer — tabbed side-drawer for a single agent run row.
 *
 * Tabs:
 *   1. Output     — response_text rendered via <Markdown>
 *   2. Sources    — <source id="...">...</source> blocks parsed from the
 *                   prompt (not yet exposed by backend; Wave B-UI Task 9).
 *   3. Citations  — parsed citations_json list
 *   4. Cost & telemetry — token/cost table
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
import type { AgentActivityRow } from "@/lib/api";

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

function SourcesTab() {
  return (
    <p className="text-sm text-muted-foreground italic">
      Sources: not captured for this run yet (see Wave B-UI Task 9 for backend
      exposure).
    </p>
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

            <Tabs defaultValue="output" className="flex-1 overflow-hidden">
              <TabsList className="w-full justify-start">
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

              <TabsContent value="output" className="overflow-y-auto">
                <OutputTab row={row} />
              </TabsContent>

              <TabsContent value="sources">
                <SourcesTab />
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

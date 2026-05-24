"use client";

/**
 * Markdown renderer used by Plan view + intake conversation + advisor
 * question_for_user.
 *
 * Plugins enabled (lazy-loaded so a missing dep doesn't break SSR):
 *
 *   - `remark-breaks`: honors single-`\n` line breaks. Without it,
 *     CommonMark collapses single newlines to a space and an LLM-emitted
 *     multi-line list renders as one wall of text. Most Claude outputs
 *     use single `\n` between numbered items.
 *
 *   - `remark-gfm`: GitHub Flavored Markdown — adds pipe-tables,
 *     strikethrough, task lists, autolinks. The advisor frequently
 *     emits tabular data (file inventories, account summaries) using
 *     pipe-table syntax which would otherwise render as raw text.
 *
 * Component overrides:
 *
 *   - `p`: paragraphs whose first text node starts with `Q#N — ` get
 *     distinctive treatment (mono accent prefix + tinted background +
 *     left border). The advisor agent batches user questions with this
 *     `Q#N — ...` pattern; the styling makes each one visually
 *     separable from preceding context-update notes.
 */

import * as React from "react";

interface MarkdownProps {
  children: string;
}

interface ReactMarkdownProps {
  children: string;
  remarkPlugins?: unknown[];
  components?: Record<string, unknown>;
}

// Detect a paragraph starting with `Q#N — ` (or `Q#N - `, `Q#N. `, etc.)
// where N is one or more digits. The advisor agent emits exactly this
// shape; we lean on it for the visual treatment.
const Q_PREFIX_RE = /^Q#(\d+)\s*[—\-:.]\s+/;

interface QMatch {
  number: string;
  rest: React.ReactNode;
}

function matchQPrefix(children: React.ReactNode): QMatch | null {
  // react-markdown gives us a ReactNode tree. The Q-prefix lives in the
  // FIRST text child of the paragraph. We only inspect that one — if the
  // first child isn't a plain string, bail.
  const arr = React.Children.toArray(children);
  if (arr.length === 0) return null;
  const first = arr[0];
  if (typeof first !== "string") return null;
  const m = first.match(Q_PREFIX_RE);
  if (!m) return null;
  const remainder = first.slice(m[0].length);
  // Rebuild the children with the prefix stripped from the first text node.
  const rest = [remainder, ...arr.slice(1)];
  return { number: m[1], rest };
}

function QParagraph({
  number,
  rest,
}: {
  number: string;
  rest: React.ReactNode;
}) {
  return (
    <p className="my-3 rounded-md border-l-2 border-info/60 bg-info/5 pl-3 py-1.5">
      <span className="font-mono font-bold text-info mr-2">
        Q#{number}
      </span>
      <span className="text-muted-foreground mr-1.5">—</span>
      {rest}
    </p>
  );
}

export function Markdown({ children }: MarkdownProps) {
  const [bundle, setBundle] = React.useState<{
    Component: React.ComponentType<ReactMarkdownProps>;
    plugins: unknown[];
  } | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [rm, breaks, gfm] = await Promise.all([
          import("react-markdown"),
          import("remark-breaks").catch(() => null),
          import("remark-gfm").catch(() => null),
        ]);
        if (cancelled) return;
        const plugins: unknown[] = [];
        if (breaks) plugins.push(breaks.default);
        if (gfm) plugins.push(gfm.default);
        setBundle({
          Component: rm.default as React.ComponentType<ReactMarkdownProps>,
          plugins,
        });
      } catch {
        // dep not installed yet; fall back below
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (bundle) {
    const { Component, plugins } = bundle;
    const components = {
      p: ({ children: pChildren }: { children?: React.ReactNode }) => {
        const m = matchQPrefix(pChildren);
        if (m) return <QParagraph number={m.number} rest={m.rest} />;
        return <p>{pChildren}</p>;
      },
    };
    return (
      <article className="prose prose-sm dark:prose-invert max-w-none">
        <Component remarkPlugins={plugins} components={components}>
          {children}
        </Component>
      </article>
    );
  }

  return (
    <pre className="whitespace-pre-wrap text-sm text-foreground bg-muted/30 p-4 rounded-md font-sans">
      {children}
    </pre>
  );
}

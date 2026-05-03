"use client";

/**
 * Markdown renderer used by Plan view + intake conversation.
 *
 * Honors single-`\n` line breaks via the `remark-breaks` plugin.
 * Without that, CommonMark collapses single newlines to a space and
 * an LLM-emitted multi-line list renders as one wall of text. Most
 * Claude outputs use single `\n` between numbered items; we render
 * each on its own line.
 *
 * Uses lazy-loaded `react-markdown` so a missing dep doesn't break
 * SSR (paragraph-preserving plain-text fallback).
 */

import * as React from "react";

interface MarkdownProps {
  children: string;
}

interface ReactMarkdownProps {
  children: string;
  remarkPlugins?: unknown[];
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
        const [rm, breaks] = await Promise.all([
          import("react-markdown"),
          import("remark-breaks").catch(() => null),
        ]);
        if (cancelled) return;
        const plugins = breaks ? [breaks.default] : [];
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
    return (
      <article className="prose prose-sm dark:prose-invert max-w-none">
        <Component remarkPlugins={plugins}>{children}</Component>
      </article>
    );
  }

  return (
    <pre className="whitespace-pre-wrap text-sm text-foreground bg-muted/30 p-4 rounded-md font-sans">
      {children}
    </pre>
  );
}

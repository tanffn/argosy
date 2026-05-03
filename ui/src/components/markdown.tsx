"use client";

/**
 * Plan markdown renderer.
 *
 * Uses `react-markdown` when installed (the package is declared in
 * `package.json`; install with `npm install`). Falls back to a
 * paragraph-preserving plain-text rendering when the dep is absent so
 * the build never fails on a fresh checkout.
 */

import * as React from "react";

interface MarkdownProps {
  children: string;
}

export function Markdown({ children }: MarkdownProps) {
  // Lazy-load react-markdown so the absence of the dep doesn't break
  // SSR builds. Until installed we render a plain-text version.
  const [Component, setComponent] = React.useState<
    React.ComponentType<{ children: string }> | null
  >(null);

  React.useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mod = await import("react-markdown");
        if (!cancelled) {
          setComponent(() => mod.default as React.ComponentType<{ children: string }>);
        }
      } catch {
        // dep not installed yet; fall back below
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (Component) {
    return (
      <article className="prose prose-sm dark:prose-invert max-w-none">
        <Component>{children}</Component>
      </article>
    );
  }

  return (
    <pre className="whitespace-pre-wrap text-sm text-foreground bg-muted/30 p-4 rounded-md font-sans">
      {children}
    </pre>
  );
}

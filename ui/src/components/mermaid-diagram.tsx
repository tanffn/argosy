"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Renders a Mermaid source string client-side. Lazy-imports the
 * mermaid library so SSR doesn't try to evaluate `document` at build
 * time. Falls back to a plain <pre> on render error so the source is
 * still legible.
 */
export function MermaidDiagram({
  src,
  className = "",
}: {
  src: string;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!ref.current || !src.trim()) return;
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({ startOnLoad: false, theme: "dark" });
        const id = `mmd-${Math.random().toString(36).slice(2, 10)}`;
        const { svg } = await mermaid.render(id, src);
        if (!cancelled && ref.current) {
          ref.current.innerHTML = svg;
        }
      } catch (e: unknown) {
        if (!cancelled) {
          setError(String(e));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [src]);

  if (error) {
    return (
      <div className={className}>
        <p className="text-xs text-red-500 font-mono mb-2">
          Mermaid render failed: {error}
        </p>
        <pre className="text-xs font-mono bg-secondary/40 border border-border rounded-md p-3 overflow-auto">
          <code>{src}</code>
        </pre>
      </div>
    );
  }

  return <div ref={ref} className={className} aria-label="diagram" />;
}

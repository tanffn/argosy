"use client";

interface Props {
  /** Optional title; defaults to "How we compute this" */
  title?: string;
  children: React.ReactNode;
}

/**
 * Methodology panel — wraps prose + (optional) formula blocks inside a
 * <DrilldownSection title="Methodology"> at the bottom of a retirement page.
 *
 * Renders children with prose styles. Callers supply React nodes
 * (paragraphs, lists, formula blocks). Keep blocks short — link to
 * external sources via <SourcesPanel/> rather than inlining citations.
 */
export function MethodologyPanel({ title = "How we compute this", children }: Props) {
  return (
    <div className="space-y-2">
      <h4 className="text-sm font-medium">{title}</h4>
      <div className="prose prose-sm prose-invert max-w-none text-muted-foreground">
        {children}
      </div>
    </div>
  );
}

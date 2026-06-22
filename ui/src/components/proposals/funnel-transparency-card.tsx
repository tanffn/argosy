"use client";

// Fetch-on-mount pattern: one effect pulls the latest funnel run + its
// narrative (matches the rest of /proposals, pre-React-Query). State is set
// inside the effect behind a `cancelled` guard.

import { useEffect, useState } from "react";

import { CollapsibleSection } from "@/components/ui/collapsible-section";
import { api, type FunnelNarrative } from "@/lib/api";

/**
 * "What Argosy did for me" — the collapsed transparency view of the daily
 * decision funnel. Self-resolved work (most names: no action) is summarised
 * here and never pushed to the active to-do list
 * (feedback_client_in_loop_only_when_needed). Renders nothing when the funnel
 * has never run, so it's an invisible scroll target until there's something to
 * show.
 */
export function FunnelTransparencyCard({ userId }: { userId: string }) {
  const [narrative, setNarrative] = useState<FunnelNarrative | null>(null);
  const [shadow, setShadow] = useState(false);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .funnelRuns(userId, 1)
      .then(async (r) => {
        const latest = r.runs?.[0];
        if (!latest) {
          if (!cancelled) setLoaded(true);
          return;
        }
        const n = await api.funnelRunNarrative(userId, latest.run_id);
        if (!cancelled) {
          setNarrative(n);
          setShadow(latest.shadow);
          setLoaded(true);
        }
      })
      .catch(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  // Nothing to show until the funnel has produced at least one run.
  if (!loaded || !narrative) return null;

  const c = narrative.counts;
  const summary =
    `${c.proposed} proposed · ${c.deep_reviewed} reviewed · ` +
    `${c.no_action} no action${shadow ? " · shadow" : ""}`;

  return (
    <section id="what-argosy-did" className="scroll-mt-6">
      <CollapsibleSection title="What Argosy did for me" summary={summary}>
        <div className="flex flex-col gap-3 px-1 py-1">
          {shadow && (
            <p className="text-xs text-muted-foreground">
              Argosy&apos;s daily review is still warming up — it checks the
              market and your holdings each day and records what it would
              suggest, but isn&apos;t putting suggestions in front of you yet
              while it learns what&apos;s worth your attention.
            </p>
          )}
          <p className="text-sm">{narrative.headline}</p>
          {narrative.as_of && (
            <p className="text-xs text-muted-foreground">as of {narrative.as_of}</p>
          )}
          {narrative.proposed.length > 0 && (
            <ul className="flex flex-col gap-1 text-sm">
              {narrative.proposed.map((p, i) => (
                <li key={i} className="text-muted-foreground">
                  <span className="text-foreground font-medium">{p.subject}</span>
                  {p.reason ? ` — ${p.reason}` : null}
                </li>
              ))}
            </ul>
          )}
          <p className="text-xs text-muted-foreground">
            Every holding it looked at — and why it did or didn&apos;t act — is
            kept on record so you can always check its work.
          </p>
        </div>
      </CollapsibleSection>
    </section>
  );
}

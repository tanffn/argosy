"use client";

import { Markdown } from "@/components/markdown";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { DraftResponse } from "@/lib/api";

/**
 * PlanFullDetailCard — surfaces the COMPLETE synthesized plan markdown for every
 * horizon: targets, themes, actions, AND the appendices that live in the
 * long-horizon markdown (trajectory & retirement-age reconciliation, the A1-A15
 * assumption ledger, the show-your-work number derivations, the currency-
 * mismatch / FX-risk block, and the section-by-section evidence tree).
 *
 * The recap's Full-Plan card shows the plain-English narrative; this card sits
 * beneath it so the auditable derivation appendix is visible directly on /plan
 * — not only in the export or the /decisions audit. Output-trust doctrine:
 * every number on the page drills down to its readable raw build-up.
 */
export function PlanFullDetailCard({
  structured,
}: {
  structured: DraftResponse | null;
}) {
  if (!structured) return null;

  const sections: ReadonlyArray<readonly [string, string | null]> = [
    [
      "Long horizon — incl. trajectory, assumption ledger, number derivations, FX-risk & evidence appendices",
      structured.horizon_long_md,
    ],
    ["Medium horizon", structured.horizon_medium_md],
    ["Short horizon", structured.horizon_short_md],
  ];
  const present = sections.filter(([, md]) => md && md.trim().length > 0);
  if (present.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Full plan detail &amp; derivation appendix</CardTitle>
        <CardDescription>
          Every horizon&apos;s structured markdown — targets, themes, actions, the
          show-your-work number derivations, the assumption ledger, the FX-risk
          block, and the section-by-section evidence. Nothing hidden behind the
          narrative.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {present.map(([title, md], i) => (
          <details
            key={title}
            open={i === 0}
            className="rounded-lg border border-border/50 p-3"
          >
            <summary className="cursor-pointer text-sm font-medium">
              {title}
            </summary>
            <article className="prose prose-sm dark:prose-invert max-w-none mt-3 overflow-x-auto">
              <Markdown>{md as string}</Markdown>
            </article>
          </details>
        ))}
      </CardContent>
    </Card>
  );
}

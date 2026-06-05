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
 * PlanFullDetailCard — the auditable derivation appendix for the current plan.
 *
 * The recap's Full-Plan card already renders the plain-English narrative (and,
 * as a fallback, the per-horizon targets/themes/actions prose). This card sits
 * beneath it and surfaces only the ``## Appendix — *`` sub-sections appended to
 * the long-horizon markdown — the A1-A15 assumption ledger, the show-your-work
 * number derivations, the trajectory & retirement-age reconciliation, the
 * currency-mismatch / FX-risk block, the section-by-section evidence tree, and
 * the Fleet receipts. Each appendix is an individually collapsible audit
 * surface, closed by default, so the prose isn't double-rendered and the page
 * doesn't dump every derivation fully expanded.
 *
 * Output-trust doctrine: every number on the page drills down to its readable
 * raw build-up — here, one disclosure per appendix.
 */

interface MarkdownSegment {
  heading: string;
  body: string;
}

// Split markdown into {heading, body} segments on its level-2 ``## `` heading
// boundaries. Any leading content before the first ``## `` heading is returned
// under an empty heading. Only ``## `` (exactly two hashes) starts a new
// segment, so ``###`` sub-headings stay inside their parent's body.
function splitOnLevel2Headings(md: string): MarkdownSegment[] {
  const lines = md.split("\n");
  const segments: MarkdownSegment[] = [];
  let heading = "";
  let body: string[] = [];
  const flush = () => {
    const bodyText = body.join("\n").trim();
    if (heading || bodyText) {
      segments.push({ heading, body: bodyText });
    }
  };
  for (const line of lines) {
    const m = /^##\s+(?!#)(.*)$/.exec(line);
    if (m) {
      flush();
      heading = m[1].trim();
      body = [];
    } else {
      body.push(line);
    }
  }
  flush();
  return segments;
}

// A segment is an appendix when its heading begins with "Appendix" (the
// synthesizer emits ``## Appendix — <name>``).
function isAppendix(seg: MarkdownSegment): boolean {
  return /^appendix\b/i.test(seg.heading);
}

export function PlanFullDetailCard({
  structured,
}: {
  structured: DraftResponse | null;
}) {
  if (!structured) return null;

  // The appendices live in the long-horizon markdown; the medium/short
  // horizons carry only targets/themes/actions (already in the narrative),
  // so we mine the long markdown for ``## Appendix — *`` segments only.
  const longMd = structured.horizon_long_md;
  const appendices =
    longMd && longMd.trim().length > 0
      ? splitOnLevel2Headings(longMd).filter(isAppendix)
      : [];

  if (appendices.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          Derivation appendix &amp; evidence
        </CardTitle>
        <CardDescription>
          The auditable build-up behind the plan — the assumption ledger, the
          show-your-work number derivations, the trajectory &amp;
          retirement-age reconciliation, the FX-risk block, the
          section-by-section evidence, and the Fleet receipts. Each section is
          collapsed; open the ones you want to audit. The plain-English plan
          itself is in the card above.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {appendices.map((seg) => (
          <details
            key={seg.heading}
            className="rounded-lg border border-border/50 p-3"
          >
            <summary className="cursor-pointer text-sm font-medium">
              {seg.heading}
            </summary>
            <article className="prose prose-sm dark:prose-invert max-w-none mt-3 overflow-x-auto">
              <Markdown>{seg.body}</Markdown>
            </article>
          </details>
        ))}
      </CardContent>
    </Card>
  );
}

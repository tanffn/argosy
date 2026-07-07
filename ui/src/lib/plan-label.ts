/**
 * Human-readable plan name from the internal PlanVersion slug.
 *
 * PlanVersion carries no title field — version_label is a machine slug
 * like "x10-sleeve-draft-20260706-124710" or
 * "refinement-draft-2026-07-06-052139". Client-side formatting is the
 * smallest correct change (adding a title column is a schema/backend
 * concern): strip the timestamp suffix and draft/refinement plumbing
 * words, and compose "Plan v<N> · <short label> · <Mon YYYY>".
 */

export interface PlanLabelSource {
  plan_version_id: number | null;
  version_label: string | null;
  imported_at: string | null;
}

/** "x10-sleeve-draft-20260706-124710" → "x10 sleeve". */
export function shortPlanLabel(slug: string): string {
  let s = slug.trim();
  // Trailing timestamp stamps: -YYYYMMDD-HHMMSS or -YYYY-MM-DD-HHMMSS.
  s = s.replace(/-\d{8}-\d{6}$/, "").replace(/-\d{4}-\d{2}-\d{2}-\d{6}$/, "");
  // Drop pipeline words wherever they sit ("draft", "plan").
  const words = s
    .split(/[-_]+/)
    .filter((w) => w.length > 0 && !["draft", "plan"].includes(w.toLowerCase()));
  return words.join(" ").trim();
}

/** Full display name: "Plan v67 · x10 sleeve · Jul 2026". */
export function formatPlanLabel(plan: PlanLabelSource | null): string | null {
  if (!plan) return null;
  const parts: string[] = [];
  if (plan.plan_version_id !== null) parts.push(`Plan v${plan.plan_version_id}`);
  const short = plan.version_label ? shortPlanLabel(plan.version_label) : "";
  if (short) parts.push(short);
  if (plan.imported_at) {
    const d = new Date(plan.imported_at);
    if (!Number.isNaN(d.getTime())) {
      parts.push(
        d.toLocaleDateString([], { month: "short", year: "numeric" }),
      );
    }
  }
  // Fall back to the raw slug rather than rendering nothing.
  if (parts.length === 0) return plan.version_label || null;
  return parts.join(" · ");
}

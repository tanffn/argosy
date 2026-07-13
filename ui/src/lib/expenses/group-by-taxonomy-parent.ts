import type { CategorySpend } from "@/lib/expenses/api";

export interface DisplayCategory {
  key: string;
  /** Leaf slug used for the transactions deep-link; null for synthetic
   *  parent rows (children keep their own links). */
  slug: string | null;
  label_en: string;
  total_nis: number;
  transaction_count: number;
  percent: number;
  depth: 0 | 1;
  /** Set on depth-0 parent rows: keys of the leaves nested under it. */
  childKeys?: string[];
  /** Set on depth-1 rows: key of the parent row they belong to. */
  parentKey?: string;
}

/** Stable React key for a synthetic taxonomy-parent row. Must never equal
 *  a leaf slug — parent categories (e.g. ``transportation``) can themselves
 *  carry spend, and that leaf would otherwise collide with the group row. */
export function parentRowKey(parentSlug: string): string {
  return `parent:${parentSlug}`;
}

/** Inverse of ``parentRowKey`` for bar-color hashing; passthrough otherwise. */
export function slugForColor(rowKey: string, slug: string | null): string {
  if (slug) return slug;
  return rowKey.startsWith("parent:") ? rowKey.slice("parent:".length) : rowKey;
}

/** Group leaves under their taxonomy parents (backend-supplied
 *  parent_slug/parent_label). Parents with 2+ spending leaves become
 *  collapsible rows summing their children; single-leaf parents stay
 *  flat (no point folding "Groceries" under "Food") unless the parent
 *  slug itself also has spend — then we still group so totals stay
 *  correct and React keys stay unique. Leaves without a parent render
 *  flat. */
export function groupByTaxonomyParent(
  cats: CategorySpend[],
  spendingTotal: number,
): DisplayCategory[] {
  const denom = spendingTotal || 1;
  const byParent = new Map<string, CategorySpend[]>();
  const flat: CategorySpend[] = [];
  for (const c of cats) {
    if (c.parent_slug) {
      const group = byParent.get(c.parent_slug) ?? [];
      group.push(c);
      byParent.set(c.parent_slug, group);
    } else {
      flat.push(c);
    }
  }

  // Spend booked directly on a taxonomy parent (slug has no parent_slug,
  // but other leaves point at it) must not render as a second top-level
  // row keyed by that slug — that duplicates the synthetic parent key and
  // breaks expand/collapse under React's list reconciliation.
  const parentLevelSpend = new Map<string, CategorySpend>();
  const trueFlat: CategorySpend[] = [];
  for (const c of flat) {
    if (byParent.has(c.slug)) {
      parentLevelSpend.set(c.slug, c);
    } else {
      trueFlat.push(c);
    }
  }

  const topLevel: DisplayCategory[] = trueFlat.map((c) => ({
    key: c.slug,
    slug: c.slug,
    label_en: c.label_en,
    total_nis: c.total_nis,
    transaction_count: c.transaction_count,
    percent: c.percent,
    depth: 0,
  }));
  const childrenByParent = new Map<string, DisplayCategory[]>();

  for (const [parentSlug, kids] of byParent) {
    const orphan = parentLevelSpend.get(parentSlug);
    // Group when there are 2+ leaves, or when parent-level spend would
    // otherwise collide / disappear beside a single leaf.
    if (kids.length === 1 && !orphan) {
      const c = kids[0];
      topLevel.push({
        key: c.slug,
        slug: c.slug,
        label_en: c.label_en,
        total_nis: c.total_nis,
        transaction_count: c.transaction_count,
        percent: c.percent,
        depth: 0,
      });
      continue;
    }
    let total = kids.reduce((s, c) => s + c.total_nis, 0);
    let count = kids.reduce((s, c) => s + c.transaction_count, 0);
    if (orphan) {
      total += orphan.total_nis;
      count += orphan.transaction_count;
    }
    const pKey = parentRowKey(parentSlug);
    topLevel.push({
      key: pKey,
      slug: null,
      label_en: kids[0].parent_label ?? orphan?.label_en ?? parentSlug,
      total_nis: total,
      transaction_count: count,
      percent: (total / denom) * 100,
      depth: 0,
      childKeys: kids.map((c) => c.slug),
    });
    childrenByParent.set(
      pKey,
      [...kids]
        .sort((a, b) => b.total_nis - a.total_nis)
        .map((c) => ({
          key: c.slug,
          slug: c.slug,
          label_en: c.label_en,
          total_nis: c.total_nis,
          transaction_count: c.transaction_count,
          percent: c.percent,
          depth: 1 as const,
          parentKey: pKey,
        })),
    );
  }

  topLevel.sort((a, b) => b.total_nis - a.total_nis);
  const ordered: DisplayCategory[] = [];
  for (const p of topLevel) {
    ordered.push(p);
    const kids = childrenByParent.get(p.key);
    if (kids) ordered.push(...kids);
  }
  return ordered;
}

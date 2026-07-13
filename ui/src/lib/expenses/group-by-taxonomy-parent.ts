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

function leafRow(
  c: CategorySpend,
  depth: 0 | 1,
  parentKey?: string,
): DisplayCategory {
  return {
    key: c.slug,
    slug: c.slug,
    label_en: c.label_en,
    total_nis: c.total_nis,
    transaction_count: c.transaction_count,
    percent: c.percent,
    depth,
    parentKey,
  };
}

/** Depth-1 row for spend booked directly on a taxonomy parent slug.
 *  Deep-links to that parent slug; key is prefixed so it never collides
 *  with a leaf that shares the same slug string. */
function unspecifiedParentSpendRow(
  orphan: CategorySpend,
  parentKey: string,
): DisplayCategory {
  return {
    key: `direct:${orphan.slug}`,
    slug: orphan.slug,
    label_en: "Unspecified",
    total_nis: orphan.total_nis,
    transaction_count: orphan.transaction_count,
    percent: orphan.percent,
    depth: 1,
    parentKey,
  };
}

/** Group leaves under their taxonomy parents (backend-supplied
 *  parent_slug/parent_label). Parents with 2+ spending leaves become
 *  collapsible rows summing their children; single-leaf parents stay
 *  flat (no point folding "Groceries" under "Food") unless the parent
 *  slug itself also has spend — then we still group so totals stay
 *  correct and React keys stay unique. Leaves without a parent render
 *  flat.
 *
 *  Parent ``percent`` is the sum of its children's backend percents
 *  (same basis as the leaves), not recomputed from spendingTotal. */
export function groupByTaxonomyParent(
  cats: CategorySpend[],
  _spendingTotal?: number,
): DisplayCategory[] {
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

  const topLevel: DisplayCategory[] = trueFlat.map((c) => leafRow(c, 0));
  const childrenByParent = new Map<string, DisplayCategory[]>();

  for (const [parentSlug, kids] of byParent) {
    const orphan = parentLevelSpend.get(parentSlug);
    // Group when there are 2+ leaves, or when parent-level spend would
    // otherwise collide / disappear beside a single leaf.
    if (kids.length === 1 && !orphan) {
      topLevel.push(leafRow(kids[0], 0));
      continue;
    }
    const pKey = parentRowKey(parentSlug);
    const childRows: DisplayCategory[] = [...kids]
      .sort((a, b) => b.total_nis - a.total_nis)
      .map((c) => leafRow(c, 1, pKey));
    if (orphan) {
      childRows.push(unspecifiedParentSpendRow(orphan, pKey));
      childRows.sort((a, b) => b.total_nis - a.total_nis);
    }
    const total = childRows.reduce((s, c) => s + c.total_nis, 0);
    const count = childRows.reduce((s, c) => s + c.transaction_count, 0);
    const percent = childRows.reduce((s, c) => s + c.percent, 0);
    topLevel.push({
      key: pKey,
      slug: null,
      label_en: kids[0].parent_label ?? orphan?.label_en ?? parentSlug,
      total_nis: total,
      transaction_count: count,
      percent,
      depth: 0,
    });
    childrenByParent.set(pKey, childRows);
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

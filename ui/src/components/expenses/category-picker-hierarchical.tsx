// ui/src/components/expenses/category-picker-hierarchical.tsx
"use client";

import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

import type { CategoryOut } from "@/lib/expenses/api";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  categories: CategoryOut[];
  currentSlug: string | null;
  onPick: (slug: string) => void | Promise<void>;
  onAddSubCategoryClick?: () => void;
  busySlug?: string | null;
}

interface TreeNode {
  cat: CategoryOut;
  children: TreeNode[];
}

function buildTree(cats: CategoryOut[]): TreeNode[] {
  const byParent = new Map<string | null, CategoryOut[]>();
  for (const c of cats) {
    const k = c.parent_slug ?? null;
    if (!byParent.has(k)) byParent.set(k, []);
    byParent.get(k)!.push(c);
  }
  function build(parentSlug: string | null): TreeNode[] {
    return (byParent.get(parentSlug) ?? []).map((cat) => ({
      cat,
      children: build(cat.slug),
    }));
  }
  return build(null);
}

export function HierarchicalCategoryPicker({
  open, onOpenChange, categories, currentSlug, onPick,
  onAddSubCategoryClick, busySlug,
}: Props) {
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    // Default: expand every parent.
    return new Set(categories.filter((c) => !c.parent_slug).map((c) => c.slug));
  });

  const tree = useMemo(() => buildTree(categories), [categories]);

  const matches = useMemo(() => {
    if (!filter.trim()) return null;
    const q = filter.toLowerCase();
    return new Set(
      categories
        .filter(
          (c) =>
            c.slug.toLowerCase().includes(q) ||
            c.label_en.toLowerCase().includes(q),
        )
        .map((c) => c.slug),
    );
  }, [filter, categories]);

  function toggle(slug: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  }

  function renderNode(n: TreeNode, depth: number): React.ReactNode {
    const visibleByFilter = matches === null || matches.has(n.cat.slug)
      || n.children.some((c) => visibleSubtree(c));
    if (!visibleByFilter) return null;
    const hasChildren = n.children.length > 0;
    const isExpanded = expanded.has(n.cat.slug) || matches !== null;
    const matchesFilter = matches === null || matches.has(n.cat.slug);
    return (
      <div key={n.cat.slug}>
        <div className="flex items-center gap-2" style={{ paddingLeft: depth * 12 }}>
          {hasChildren ? (
            <button
              type="button"
              className="text-xs w-4 text-muted-foreground"
              onClick={() => toggle(n.cat.slug)}
              aria-label={isExpanded ? "Collapse" : "Expand"}
            >
              {isExpanded ? "▾" : "▸"}
            </button>
          ) : (
            <span className="w-4" />
          )}
          <Button
            variant={n.cat.slug === currentSlug ? "secondary" : "ghost"}
            size="sm"
            disabled={busySlug !== null && busySlug !== undefined}
            onClick={() => onPick(n.cat.slug)}
            className="justify-start flex-1 capitalize"
          >
            <span className={matchesFilter ? "" : "opacity-60"}>
              {busySlug === n.cat.slug ? "Saving…" : n.cat.label_en}
            </span>
            <span className="ml-auto text-xs text-muted-foreground">
              {n.cat.slug}
            </span>
          </Button>
        </div>
        {hasChildren && isExpanded && (
          <div>
            {n.children.map((c) => renderNode(c, depth + 1))}
          </div>
        )}
      </div>
    );
  }

  function visibleSubtree(n: TreeNode): boolean {
    if (matches === null) return true;
    if (matches.has(n.cat.slug)) return true;
    return n.children.some(visibleSubtree);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Pick category</DialogTitle>
        </DialogHeader>
        <Input
          placeholder="Filter categories…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          autoFocus
        />
        <div className="max-h-80 overflow-y-auto flex flex-col gap-0.5">
          {tree.map((n) => renderNode(n, 0))}
        </div>
        {onAddSubCategoryClick && (
          <div className="pt-2 border-t border-border flex justify-end">
            <Button
              variant="outline"
              size="sm"
              onClick={onAddSubCategoryClick}
            >
              + Add sub-category
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

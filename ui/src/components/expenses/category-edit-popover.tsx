"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  expensesApi, type CategoryOut,
} from "@/lib/expenses/api";

interface CategoryEditPopoverProps {
  txId: number;
  userId: string;
  currentSlug: string | null;
  categories: CategoryOut[];
  onChanged?: (newSlug: string) => void;
}

export function CategoryEditPopover({
  txId, userId, currentSlug, categories, onChanged,
}: CategoryEditPopoverProps) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [saving, setSaving] = useState<string | null>(null);

  const filtered = categories.filter((c) =>
    c.slug.includes(filter.toLowerCase()) || c.label_en.toLowerCase().includes(filter.toLowerCase())
  );

  async function pick(slug: string) {
    setSaving(slug);
    try {
      await expensesApi.patchTransactionCategory(txId, userId, slug);
      onChanged?.(slug);
      setOpen(false);
      setFilter("");
    } catch (e) {
      alert(`Failed to save: ${e}`);
    } finally {
      setSaving(null);
    }
  }

  return (
    <>
      <Badge
        variant="secondary"
        className="cursor-pointer hover:bg-secondary/80 capitalize"
        onClick={() => setOpen(true)}
      >
        {currentSlug?.replace(/_/g, " ") ?? "uncategorized"}
      </Badge>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Re-categorize</DialogTitle>
          </DialogHeader>
          <Input
            placeholder="Filter categories…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            autoFocus
          />
          <div className="max-h-72 overflow-y-auto flex flex-col gap-1">
            {filtered.map((c) => (
              <Button
                key={c.slug}
                variant={c.slug === currentSlug ? "secondary" : "ghost"}
                size="sm"
                disabled={saving !== null}
                onClick={() => pick(c.slug)}
                className="justify-start capitalize"
              >
                {saving === c.slug ? "Saving…" : c.label_en}
              </Button>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

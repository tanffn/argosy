"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";

import { categoriesApi, type CategoryOut } from "@/lib/expenses/api";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  userId: string;
  categories: CategoryOut[];           // all current categories
  onCreated: (newCat: CategoryOut) => void;
}

export function AddSubCategoryDialog({
  open, onOpenChange, userId, categories, onCreated,
}: Props) {
  const topLevel = categories.filter((c) => !c.parent_slug);
  const [parentSlug, setParentSlug] = useState<string>(topLevel[0]?.slug ?? "");
  const [slug, setSlug] = useState("");
  const [labelEn, setLabelEn] = useState("");
  const [labelHe, setLabelHe] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function submit() {
    setError(null);
    if (!parentSlug || !slug.trim() || !labelEn.trim()) {
      setError("Parent, slug, and English label are required.");
      return;
    }
    if (slug.includes(".")) {
      setError("Slug must not contain '.'");
      return;
    }
    setSaving(true);
    try {
      const created = await categoriesApi.create({
        user_id: userId,
        parent_slug: parentSlug,
        slug: slug.trim(),
        label_en: labelEn.trim(),
        label_he: labelHe.trim() || undefined,
      });
      onCreated({
        id: created.id,
        slug: created.slug,
        label_en: created.label_en,
        label_he: created.label_he,
        parent_slug: created.parent_slug,
        is_excluded_from_spend: created.is_excluded_from_spend,
        is_inflow: created.is_inflow,
      });
      setSlug(""); setLabelEn(""); setLabelHe(""); setError(null);
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Add sub-category</DialogTitle>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div>
            <Label htmlFor="parent">Parent</Label>
            <Select value={parentSlug} onValueChange={setParentSlug}>
              <SelectTrigger id="parent">
                <SelectValue placeholder="Pick a parent" />
              </SelectTrigger>
              <SelectContent>
                {topLevel.map((c) => (
                  <SelectItem key={c.slug} value={c.slug}>
                    {c.label_en} ({c.slug})
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <Label htmlFor="slug">Slug</Label>
            <Input
              id="slug"
              value={slug}
              onChange={(e) => setSlug(e.target.value)}
              placeholder="health"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Will be stored as <code>{parentSlug}.{slug || "<slug>"}</code>
            </p>
          </div>
          <div>
            <Label htmlFor="label_en">Label (English)</Label>
            <Input
              id="label_en"
              value={labelEn}
              onChange={(e) => setLabelEn(e.target.value)}
              placeholder="Health Insurance"
            />
          </div>
          <div>
            <Label htmlFor="label_he">Label (Hebrew, optional)</Label>
            <Input
              id="label_he"
              value={labelHe}
              onChange={(e) => setLabelHe(e.target.value)}
              placeholder="ביטוח בריאות"
            />
          </div>
          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={saving}>
            {saving ? "Saving…" : "Add"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

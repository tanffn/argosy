// ui/src/components/expenses/label-editor.tsx
"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { HierarchicalCategoryPicker } from "./category-picker-hierarchical";
import type { CategoryOut } from "@/lib/expenses/api";

export type LabelEditorMode = "single-tx" | "bulk-tx";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: LabelEditorMode;
  categories: CategoryOut[];
  currentSlug?: string | null;
  currentTags?: string[];
  showSiblingsCheckbox: boolean; // only for single-tx mode on transactions page
  /** For bulk-tx mode: number of transactions the action will affect.
   * Shown in the dialog title so the user is reminded of the scope. */
  bulkCount?: number;
  onSubmit: (payload: {
    categorySlug?: string;
    addTags: string[];
    removeTags: string[];
    applyToSiblings: boolean;
  }) => Promise<void>;
  /** Optional: when set, the embedded picker shows a "+ Add sub-category"
   * button that fires this callback. The parent owns the AddSubCategoryDialog. */
  onAddSubCategoryClick?: () => void;
}

export function LabelEditor({
  open, onOpenChange, mode, categories, currentSlug, currentTags = [],
  showSiblingsCheckbox, bulkCount, onSubmit, onAddSubCategoryClick,
}: Props) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const [chosenSlug, setChosenSlug] = useState<string | undefined>(undefined);
  const [tagInput, setTagInput] = useState("");
  const [addedTags, setAddedTags] = useState<string[]>([]);
  const [removedTags, setRemovedTags] = useState<string[]>([]);
  const [applyToSiblings, setApplyToSiblings] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function addTag() {
    const t = tagInput.trim();
    if (!t) return;
    if (!addedTags.includes(t)) setAddedTags((xs) => [...xs, t]);
    setTagInput("");
  }
  function dropAdded(t: string) {
    setAddedTags((xs) => xs.filter((x) => x !== t));
  }
  function toggleRemoveExisting(t: string) {
    setRemovedTags((xs) =>
      xs.includes(t) ? xs.filter((x) => x !== t) : [...xs, t],
    );
  }

  async function submit() {
    setError(null);
    // Auto-commit pending input — common confusion: type a tag, click Save
    // without pressing Enter/Add first. We treat the input contents as an
    // implicit add at submit time so the user's intent isn't lost.
    const pending = tagInput.trim();
    const effectiveAddTags =
      pending && !addedTags.includes(pending) ? [...addedTags, pending] : addedTags;
    if (!chosenSlug && effectiveAddTags.length === 0 && removedTags.length === 0) {
      setError("Pick a category or add/remove at least one tag.");
      return;
    }
    setSaving(true);
    try {
      await onSubmit({
        categorySlug: chosenSlug,
        addTags: effectiveAddTags,
        removeTags: removedTags,
        applyToSiblings,
      });
      setChosenSlug(undefined); setAddedTags([]); setRemovedTags([]);
      setTagInput("");
      setApplyToSiblings(false);
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
          <DialogTitle>
            {mode === "bulk-tx"
              ? `Set labels — ${bulkCount ?? "?"} transaction${bulkCount === 1 ? "" : "s"}`
              : "Set labels"}
          </DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div>
            <Label>Category</Label>
            <Button
              variant="outline"
              className="w-full justify-start"
              onClick={() => setPickerOpen(true)}
            >
              {chosenSlug ?? currentSlug ?? "Pick a category…"}
            </Button>
          </div>

          {mode === "single-tx" && currentTags.length > 0 && (
            <div>
              <Label>Existing tags</Label>
              <div className="flex flex-wrap gap-1">
                {currentTags.map((t) => {
                  const removing = removedTags.includes(t);
                  return (
                    <Badge
                      key={t}
                      variant={removing ? "destructive" : "secondary"}
                      onClick={() => toggleRemoveExisting(t)}
                      className="cursor-pointer"
                    >
                      {t} {removing && "×"}
                    </Badge>
                  );
                })}
              </div>
            </div>
          )}

          <div>
            <Label>Add tags</Label>
            <div className="flex gap-2">
              <Input
                value={tagInput}
                onChange={(e) => setTagInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") { e.preventDefault(); addTag(); }
                }}
                placeholder="e.g. trip:greece-2026-aug"
              />
              <Button variant="outline" onClick={addTag}>Add</Button>
            </div>
            {addedTags.length > 0 && (
              <div className="flex flex-wrap gap-1 mt-2">
                {addedTags.map((t) => (
                  <Badge
                    key={t}
                    variant="default"
                    className="cursor-pointer"
                    onClick={() => dropAdded(t)}
                  >
                    + {t} ×
                  </Badge>
                ))}
              </div>
            )}
          </div>

          {showSiblingsCheckbox && mode === "single-tx" && (
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={applyToSiblings}
                onCheckedChange={(c) => setApplyToSiblings(c === true)}
              />
              Apply to all sibling transactions of this merchant
            </label>
          )}

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>

        <HierarchicalCategoryPicker
          open={pickerOpen}
          onOpenChange={setPickerOpen}
          categories={categories}
          currentSlug={chosenSlug ?? currentSlug ?? null}
          onPick={(slug) => { setChosenSlug(slug); setPickerOpen(false); }}
          onAddSubCategoryClick={onAddSubCategoryClick}
        />
      </DialogContent>
    </Dialog>
  );
}

"use client";

import { useEffect, useState } from "react";

import { TagChip } from "@/components/expenses/tag-chip";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { expensesApi } from "@/lib/expenses/api";

interface TagEditorProps {
  txId: number;
  userId: string;
  currentTags: string[];
  onChanged?: (tags: string[]) => void;
  label?: string;          // overrides the trigger label; default '+ tag'
}

/**
 * Popover for editing tags on a single transaction:
 *   - shows current tags with × to remove
 *   - text input + autocomplete suggestions sourced from /api/expenses/tags
 *   - quick-select for existing tags or 'Create "trip:foo"' on Enter.
 *
 * The component is uncontrolled w.r.t. the rendered tag list — it talks
 * directly to the API and reports back through `onChanged`. The popover
 * stays in sync on the next refresh by the parent.
 */
export function TagEditor({
  txId, userId, currentTags, onChanged, label,
}: TagEditorProps) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  // Local optimistic state — seeded from props on each open.
  const [localTags, setLocalTags] = useState<string[] | null>(null);
  const tags = localTags ?? currentTags;
  const [suggestions, setSuggestions] = useState<string[]>([]);

  // Reset local state whenever the popover opens (cheap; no cascade).
  function openPopover() {
    setLocalTags(null);
    setDraft("");
    setOpen(true);
  }

  useEffect(() => {
    if (!open) return;
    const prefix = draft.trim();
    let cancelled = false;
    expensesApi.listTags(userId, prefix || undefined)
      .then((r) => {
        if (cancelled) return;
        setSuggestions(r.tags.filter((t) => !tags.includes(t)));
      })
      .catch(() => { if (!cancelled) setSuggestions([]); });
    return () => { cancelled = true; };
  }, [open, draft, userId, tags]);

  async function add(tag: string) {
    const t = tag.trim();
    if (!t || tags.includes(t)) return;
    setSaving(true);
    try {
      const r = await expensesApi.addTag(txId, userId, t);
      setLocalTags(r.tags);
      onChanged?.(r.tags);
      setDraft("");
    } catch (e) {
      alert(`Failed: ${e}`);
    } finally {
      setSaving(false);
    }
  }

  async function remove(tag: string) {
    setSaving(true);
    try {
      const r = await expensesApi.removeTag(txId, userId, tag);
      setLocalTags(r.tags);
      onChanged?.(r.tags);
    } catch (e) {
      alert(`Failed: ${e}`);
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={openPopover}
        className="text-xs text-muted-foreground hover:text-foreground rounded border border-border/60 px-1.5 py-0.5 hover:bg-secondary/40"
        aria-label="Edit tags"
      >
        {label ?? "+ tag"}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Tags</DialogTitle>
          </DialogHeader>
          <div className="flex flex-wrap gap-1.5">
            {tags.length === 0 && (
              <span className="text-xs text-muted-foreground">No tags yet.</span>
            )}
            {tags.map((t) => (
              <TagChip key={t} tag={t} onRemove={() => remove(t)} />
            ))}
          </div>
          <Input
            placeholder='Type tag, e.g. "trip:greece-2026-aug"'
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && draft.trim()) {
                e.preventDefault();
                add(draft);
              }
            }}
            autoFocus
            disabled={saving}
          />
          <div className="flex flex-wrap gap-1.5 max-h-48 overflow-y-auto">
            {suggestions.map((s) => (
              <Button
                key={s}
                variant="ghost"
                size="sm"
                onClick={() => add(s)}
                disabled={saving}
                className="h-7 text-xs"
              >
                {s}
              </Button>
            ))}
            {draft.trim() && !suggestions.includes(draft.trim()) && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => add(draft)}
                disabled={saving}
                className="h-7 text-xs"
              >
                Create &quot;{draft.trim()}&quot;
              </Button>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

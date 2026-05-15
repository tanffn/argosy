"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { api, type BaselineResponse } from "@/lib/api";

type Category =
  | "goals"
  | "principles"
  | "decision_rules"
  | "targets"
  | "constraints";

interface DistillateEditDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  userId: string;
  category: Category;
  itemLabel: string;
  initialValue: string;
  fieldLabel: string; // user-facing field name (e.g. "Value", "Detail", "Rule")
  fieldKey: "value" | "detail" | "rule" | "rationale";
  onSaved: (next: BaselineResponse) => void;
}

/**
 * Inline edit dialog for one distillate item. Calls
 * PATCH /api/plan/baseline/distillate/<category>/<itemLabel>
 * and propagates the fresh BaselineResponse back to the parent.
 */
export function DistillateEditDialog(props: DistillateEditDialogProps) {
  const {
    open,
    onOpenChange,
    userId,
    category,
    itemLabel,
    initialValue,
    fieldLabel,
    fieldKey,
    onSaved,
  } = props;

  const [val, setVal] = useState(initialValue);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const body = {
        [fieldKey]: val,
        user_edit_note: note,
      } as Record<string, string>;
      const r = await api.planBaselineDistillateEdit(
        userId,
        category,
        itemLabel,
        body,
      );
      onSaved(r);
      onOpenChange(false);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            Edit {category.replace("_", " ").replace(/s$/, "")}: {itemLabel}
          </DialogTitle>
          <DialogDescription>
            Your edit will be marked user-edited and preserved through future
            re-distillations of the imported plan.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="distillate-edit-value">{fieldLabel}</Label>
            <Input
              id="distillate-edit-value"
              value={val}
              onChange={(e) => setVal(e.target.value)}
              autoFocus
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="distillate-edit-note">Note (optional)</Label>
            <Textarea
              id="distillate-edit-note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Why this edit? (e.g. 'decided to retire one year earlier')"
              rows={2}
            />
          </div>
          {error && (
            <p className="text-sm text-error font-mono">{error}</p>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={saving}
          >
            Cancel
          </Button>
          <Button onClick={onSave} disabled={saving || !val}>
            {saving ? "Saving..." : "Save edit"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

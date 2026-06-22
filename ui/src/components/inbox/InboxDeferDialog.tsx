"use client";

/**
 * InboxDeferDialog — a small, kind-agnostic "remind me later" dialog for the
 * inbox. The user picks a future date (defaults to today + 7 days) and
 * optionally types a note. Unlike the per-kind DeferModal it is not coupled to
 * any one DTO — it works for any inbox item that supports Defer.
 */

import { useEffect, useState } from "react";

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

function defaultDeferDate(): string {
  const d = new Date();
  d.setDate(d.getDate() + 7);
  return d.toISOString().slice(0, 10);
}

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string | null;
  onConfirm: (deferUntilDate: string, note: string) => Promise<void>;
}

export function InboxDeferDialog({ open, onOpenChange, title, onConfirm }: Props) {
  const [date, setDate] = useState<string>(defaultDeferDate());
  const [note, setNote] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- modal reset on open; mirrors DeferModal pattern
      setDate(defaultDeferDate());
      setNote("");
      setError(null);
    }
  }, [open]);

  async function handleConfirm() {
    setSubmitting(true);
    setError(null);
    try {
      await onConfirm(date, note);
      onOpenChange(false);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Remind me later</DialogTitle>
          <DialogDescription>
            {title ? `"${title}"` : "This item"} will come back to your inbox on
            the date you choose.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="defer-date">Bring it back on</Label>
            <Input
              id="defer-date"
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="defer-note">Note (optional)</Label>
            <Textarea
              id="defer-note"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="Why are you snoozing this?"
            />
          </div>
          {error && <p className="text-sm text-error font-mono">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleConfirm} disabled={submitting}>
            {submitting ? "Saving…" : "Remind me"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

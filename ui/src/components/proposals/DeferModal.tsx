"use client";

/**
 * DeferModal — Spec E commit #6 / spec §6.1.
 *
 * Per-row Defer action on an ActionProposal opens this modal. The
 * user picks a future date (defaults to today + 7 days) + optionally
 * types a note; the modal calls the API on confirm.
 *
 * The date is encoded into ``decided_by_user_note`` as
 * ``defer_until=<iso>; <note>`` on the backend (v1 schema ships no
 * dedicated ``defer_until`` column — see
 * argosy/services/action_proposals.py:defer_action_proposal docstring).
 * The housekeeping loop (out of v1 scope) will re-open deferred rows
 * on the requested date.
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
import type { ActionProposalDTO } from "@/lib/api";

interface DeferModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  proposal: ActionProposalDTO | null;
  onConfirm: (deferUntilDate: string, note: string) => Promise<void>;
}

function defaultDeferDate(): string {
  // Default: today + 7 days, encoded YYYY-MM-DD for <input type="date">.
  const d = new Date();
  d.setDate(d.getDate() + 7);
  return d.toISOString().slice(0, 10);
}

export function DeferModal({
  open,
  onOpenChange,
  proposal,
  onConfirm,
}: DeferModalProps) {
  const [date, setDate] = useState<string>(defaultDeferDate());
  const [note, setNote] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset state every time a new proposal opens the modal so a stale
  // date from a previous open doesn't leak across rows.
  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- modal reset on open; mirrors PushSubscriptionCard pattern
      setDate(defaultDeferDate());
      setNote("");
      setError(null);
    }
  }, [open, proposal?.id]);

  const handleConfirm = async () => {
    if (!proposal) return;
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
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Defer proposal</DialogTitle>
          <DialogDescription>
            {proposal
              ? `Remind me about "${proposal.summary}" later.`
              : "Defer this proposal to a later date."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="defer-date">Re-open on</Label>
            <Input
              id="defer-date"
              type="date"
              value={date}
              onChange={(e) => setDate(e.target.value)}
              min={new Date().toISOString().slice(0, 10)}
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <Label htmlFor="defer-note">Note (optional)</Label>
            <Textarea
              id="defer-note"
              placeholder="e.g. revisit after paycheck"
              value={note}
              onChange={(e) => setNote(e.target.value)}
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
            disabled={submitting}
          >
            Cancel
          </Button>
          <Button
            onClick={handleConfirm}
            disabled={submitting || !date}
          >
            {submitting ? "Deferring…" : "Defer"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

"use client";

/**
 * RejectModal — Spec E commit #6 / spec §6.1.
 *
 * Per-row Reject action on an ActionProposal opens this modal for a
 * free-text reason note. The note is optional; an empty note still
 * rejects the row. The reason is stored on
 * ``decided_by_user_note`` so the audit trail / future predictions-
 * ledger outcome scoring can read why the user dismissed the
 * proposal.
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
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { ActionProposalDTO } from "@/lib/api";

interface RejectModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  proposal: ActionProposalDTO | null;
  onConfirm: (reason: string) => Promise<void>;
}

export function RejectModal({
  open,
  onOpenChange,
  proposal,
  onConfirm,
}: RejectModalProps) {
  const [reason, setReason] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset state every time a new proposal opens the modal.
  useEffect(() => {
    if (open) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- modal reset on open; mirrors PushSubscriptionCard pattern
      setReason("");
      setError(null);
    }
  }, [open, proposal?.id]);

  const handleConfirm = async () => {
    if (!proposal) return;
    setSubmitting(true);
    setError(null);
    try {
      await onConfirm(reason);
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
          <DialogTitle>Reject proposal</DialogTitle>
          <DialogDescription>
            {proposal
              ? `Reject "${proposal.summary}". The reason is recorded for audit.`
              : "Reject this proposal."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="reject-reason">Reason (optional)</Label>
            <Textarea
              id="reject-reason"
              placeholder="e.g. not aligned with current target allocation"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="min-h-[80px]"
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
            variant="destructive"
            onClick={handleConfirm}
            disabled={submitting}
          >
            {submitting ? "Rejecting…" : "Reject"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

"use client";

/**
 * CustomizeModal — Spec E commit #6 / spec §6.1 / §6.2.
 *
 * Per-row Customize action on an ActionProposal opens this modal
 * with the structured suggested_payload as an editable form. The
 * UI walks ``Object.entries(suggested_payload)`` to render one
 * input per key — permissive over per-kind schema so adding a
 * payload field on the backend doesn't require a UI redeploy.
 *
 * Field-type inference:
 *
 *   * ``number`` -> numeric <Input type="number">
 *   * ``boolean`` -> checkbox
 *   * ``string`` longer than 80 chars -> <Textarea>
 *   * ``string`` shorter -> <Input type="text">
 *   * arrays / nested objects -> JSON <Textarea> (round-tripped
 *     through JSON.stringify; the user edits the raw JSON; parse
 *     errors surface inline before submit). Per-kind sophisticated
 *     editors (e.g. rebalance row picker) land in a future commit.
 *
 * On submit the modal calls onConfirm with the edited payload
 * object; the page's onCustomizeAccept handler POSTs the payload
 * to /api/proposals/actions/{id}/accept (Customize is Accept + an
 * edited payload — there is no separate "save edits" path).
 */

import { useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
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
import type { ActionProposalDTO, ActionProposalPayload } from "@/lib/api";

interface CustomizeModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  proposal: ActionProposalDTO | null;
  onConfirm: (customPayload: ActionProposalPayload) => Promise<void>;
}

type FieldKind = "string" | "longString" | "number" | "boolean" | "json";

function inferKind(value: unknown): FieldKind {
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "number") return "number";
  if (typeof value === "string") {
    return value.length > 80 ? "longString" : "string";
  }
  // arrays + objects + null go through the raw JSON editor
  return "json";
}

// String representation of any payload value for the form input.
function valueToString(value: unknown, kind: FieldKind): string {
  if (kind === "boolean") return value ? "true" : "false";
  if (value === null || value === undefined) return "";
  if (kind === "json") return JSON.stringify(value, null, 2);
  return String(value);
}

// Parse a form input back to the inferred kind. Returns either
// [parsed, null] on success or [null, errorMessage] on failure.
function parseValue(
  raw: string,
  kind: FieldKind,
): [unknown, string | null] {
  if (kind === "string" || kind === "longString") {
    return [raw, null];
  }
  if (kind === "number") {
    if (raw === "") return [null, null];
    const n = Number(raw);
    if (Number.isNaN(n)) return [null, "must be a number"];
    return [n, null];
  }
  if (kind === "boolean") {
    return [raw === "true", null];
  }
  // JSON
  if (raw.trim() === "") return [null, null];
  try {
    return [JSON.parse(raw), null];
  } catch (e) {
    return [null, `invalid JSON: ${String(e)}`];
  }
}

export function CustomizeModal({
  open,
  onOpenChange,
  proposal,
  onConfirm,
}: CustomizeModalProps) {
  // ``fields`` is the editable string representation; we parse on
  // submit. ``kinds`` is derived from the ORIGINAL payload so the
  // user editing a number to "" doesn't accidentally make us
  // re-infer it as a string.
  const originalEntries = useMemo(() => {
    if (!proposal) return [];
    return Object.entries(proposal.suggested_payload ?? {});
  }, [proposal]);

  const kinds = useMemo(() => {
    const m: Record<string, FieldKind> = {};
    for (const [k, v] of originalEntries) m[k] = inferKind(v);
    return m;
  }, [originalEntries]);

  const [fields, setFields] = useState<Record<string, string>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset state every time a new proposal opens the modal.
  useEffect(() => {
    if (open) {
      const init: Record<string, string> = {};
      for (const [k, v] of originalEntries) {
        init[k] = valueToString(v, kinds[k]);
      }
      // eslint-disable-next-line react-hooks/set-state-in-effect -- modal reset on open; mirrors PushSubscriptionCard pattern
      setFields(init);
      setFieldErrors({});
      setError(null);
    }
  }, [open, proposal?.id, originalEntries, kinds]);

  const setField = (key: string, value: string) => {
    setFields((prev) => ({ ...prev, [key]: value }));
    // Clear per-field error as user types.
    setFieldErrors((prev) => {
      if (!prev[key]) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const handleConfirm = async () => {
    if (!proposal) return;
    // Parse every field; collect errors before submit.
    const parsed: ActionProposalPayload = {};
    const errs: Record<string, string> = {};
    for (const [key] of originalEntries) {
      const [val, err] = parseValue(fields[key] ?? "", kinds[key]);
      if (err !== null) {
        errs[key] = err;
      } else {
        parsed[key] = val;
      }
    }
    if (Object.keys(errs).length > 0) {
      setFieldErrors(errs);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await onConfirm(parsed);
      onOpenChange(false);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Customize proposal</DialogTitle>
          <DialogDescription>
            {proposal
              ? `Edit the payload before accepting. Kind: ${proposal.kind}.`
              : "Edit this proposal."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3 max-h-[60vh] overflow-y-auto">
          {originalEntries.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No editable fields for this proposal kind.
            </p>
          )}
          {originalEntries.map(([key]) => {
            const kind = kinds[key];
            const inputId = `customize-${key}`;
            const errMsg = fieldErrors[key];
            return (
              <div key={key} className="flex flex-col gap-1.5">
                <Label htmlFor={inputId}>
                  <span className="font-mono">{key}</span>
                  <span className="text-xs text-muted-foreground ml-2">
                    ({kind})
                  </span>
                </Label>
                {kind === "boolean" ? (
                  <div className="flex items-center gap-2">
                    <Checkbox
                      id={inputId}
                      checked={fields[key] === "true"}
                      onCheckedChange={(c) =>
                        setField(key, c ? "true" : "false")
                      }
                    />
                    <span className="text-sm">{fields[key]}</span>
                  </div>
                ) : kind === "longString" || kind === "json" ? (
                  <Textarea
                    id={inputId}
                    value={fields[key] ?? ""}
                    onChange={(e) => setField(key, e.target.value)}
                    className={
                      kind === "json"
                        ? "font-mono text-xs min-h-[100px]"
                        : "min-h-[80px]"
                    }
                  />
                ) : (
                  <Input
                    id={inputId}
                    type={kind === "number" ? "number" : "text"}
                    value={fields[key] ?? ""}
                    onChange={(e) => setField(key, e.target.value)}
                    step={kind === "number" ? "any" : undefined}
                  />
                )}
                {errMsg && (
                  <p className="text-xs text-error font-mono">{errMsg}</p>
                )}
              </div>
            );
          })}

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
          <Button onClick={handleConfirm} disabled={submitting}>
            {submitting ? "Saving…" : "Accept with edits"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

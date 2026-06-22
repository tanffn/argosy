"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { StatusPill, type StatusPillProps } from "@/components/ui/status-pill";
import {
  api,
  type ActionItem,
  type ActionItemsResponse,
  type ActionItemStatus,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const USER_ID = "ariel";

interface ActionChecklistHeaderProps {
  /** Defaults to the single-user "ariel" to match the page convention. */
  userId?: string;
  /** Days ahead of today to surface upcoming items. Defaults to 14. */
  windowDays?: number;
}

/**
 * Plain-language "What's on you to do" checklist header for /proposals.
 *
 * This is the consolidated "what only YOU can do" view (design spec §7):
 * a quick, readable list of the dated short/medium-horizon actions from
 * the user's plan, framed in plain language with a count summary. Each row
 * carries a "How to do it" disclosure (how_to + a "Done when" criterion)
 * and a Mark-done / Undo affordance backed by the action-item ack routes.
 * Accept / reject affordances still live on the proposal cards below.
 * Reuses the canonical action-items endpoint (`api.planActionItems`), the
 * same source the Home-page widget reads.
 *
 * Renders nothing-box-free: when there are zero items it shows a quiet
 * "all caught up" line instead of an empty card.
 */
export function ActionChecklistHeader({
  userId = USER_ID,
  windowDays = 14,
}: ActionChecklistHeaderProps) {
  const [data, setData] = useState<ActionItemsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  // Per-item acknowledged override, applied optimistically on top of the
  // server payload so a mark-done / undo flips the row instantly. Keyed by
  // item_id; absent = use the server's `acknowledged` value.
  const [ackOverrides, setAckOverrides] = useState<Record<string, boolean>>({});

  useEffect(() => {
    // `loading` starts true; we only ever flip it false in finally. (Calling
    // setState synchronously in an effect body is forbidden by this Next.js's
    // react-hooks/set-state-in-effect lint rule.)
    let cancelled = false;
    api
      .planActionItems(userId, windowDays)
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setAckOverrides({});
        }
      })
      .catch(() => null)
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId, windowDays]);

  const items = data?.items ?? [];
  const total = items.length;

  const isAcked = (it: ActionItem) =>
    ackOverrides[it.item_id] ?? it.acknowledged;

  // Recompute header counts excluding acknowledged ("done") items so a row
  // the user just marked done stops counting against overdue/today.
  const liveItems = items.filter((it) => !isAcked(it));
  const overdueCount = liveItems.filter((it) => it.status === "OVERDUE").length;
  const todayCount = liveItems.filter((it) => it.status === "TODAY").length;
  const upcomingCount = liveItems.filter(
    (it) => it.status === "UPCOMING" || it.status === "DUE_SOON",
  ).length;
  const doneCount = total - liveItems.length;

  const setAck = (itemId: string, value: boolean) =>
    setAckOverrides((prev) => ({ ...prev, [itemId]: value }));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">What&apos;s on you to do</CardTitle>
        <CardDescription>
          These are the moves only you can make right now. Expand a row for how
          to do it, then mark it done when you have.
        </CardDescription>
        {data ? (
          <div className="flex items-center gap-1.5 mt-1 flex-wrap">
            {overdueCount > 0 ? (
              <StatusPill tone="error" mono>
                {overdueCount} overdue
              </StatusPill>
            ) : null}
            {todayCount > 0 ? (
              <StatusPill tone="warning" mono>
                {todayCount} today
              </StatusPill>
            ) : null}
            <StatusPill tone="neutral" mono>
              {upcomingCount} upcoming
            </StatusPill>
            {doneCount > 0 ? (
              <StatusPill tone="success" mono>
                {doneCount} done
              </StatusPill>
            ) : null}
          </div>
        ) : null}
      </CardHeader>
      <CardContent>
        {loading ? (
          <p className="text-xs text-muted-foreground font-mono">loading…</p>
        ) : total === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing waiting on you right now — you&apos;re all caught up.
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {items.map((it) => (
              <ChecklistRow
                key={it.item_id}
                item={it}
                userId={userId}
                acknowledged={isAcked(it)}
                onAckChange={(value) => setAck(it.item_id, value)}
              />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

interface ChecklistRowProps {
  item: ActionItem;
  userId: string;
  acknowledged: boolean;
  onAckChange: (value: boolean) => void;
}

function ChecklistRow({
  item,
  userId,
  acknowledged,
  onAckChange,
}: ChecklistRowProps) {
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const tone = acknowledged ? "success" : pillToneForStatus(item.status);
  const sub = item.detail || item.rationale;
  const hasDetail = Boolean(item.how_to || item.done_when);

  const handleMarkDone = async () => {
    setBusy(true);
    setError(null);
    onAckChange(true); // optimistic
    try {
      await api.planActionItemAck(userId, item.item_id, item.content_fingerprint);
    } catch {
      onAckChange(false); // revert
      setError("Couldn't mark done — try again.");
    } finally {
      setBusy(false);
    }
  };

  const handleUndo = async () => {
    setBusy(true);
    setError(null);
    onAckChange(false); // optimistic
    try {
      await api.planActionItemUnack(userId, item.item_id);
    } catch {
      onAckChange(true); // revert
      setError("Couldn't undo — try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="flex flex-col gap-1">
      <div className="flex items-start gap-3">
        <StatusPill tone={tone} mono className="mt-0.5">
          {acknowledged ? "DONE" : item.status}
        </StatusPill>
        <div className="flex-1 min-w-0">
          <p
            className={cn(
              "text-sm font-semibold leading-snug",
              acknowledged && "line-through text-muted-foreground",
            )}
          >
            {item.label}
          </p>
          {sub ? (
            <p
              className={cn(
                "text-xs text-muted-foreground truncate",
                acknowledged && "line-through",
              )}
            >
              {sub}
            </p>
          ) : null}
          {item.argosy_verified_summary ? (
            <div
              className={cn(
                "mt-1.5 rounded-md border px-2.5 py-1.5 text-xs flex items-start gap-1.5",
                item.argosy_verified
                  ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
                  : "border-amber-500/30 bg-amber-500/10 text-amber-300",
              )}
            >
              <span aria-hidden className="mt-px shrink-0 font-mono">
                {item.argosy_verified ? "✓" : "•"}
              </span>
              <span>
                <span className="font-semibold">
                  {item.argosy_verified
                    ? "Argosy verified — "
                    : "Argosy checked — "}
                </span>
                {item.argosy_verified_summary}
              </span>
            </div>
          ) : null}
          <div className="flex items-center gap-3 mt-1">
            {hasDetail ? (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="text-xs font-medium text-primary underline-offset-4 hover:underline"
                aria-expanded={expanded}
              >
                {expanded ? "Hide steps" : "How to do it"}
              </button>
            ) : null}
            {acknowledged ? (
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0 text-xs"
                disabled={busy}
                onClick={handleUndo}
              >
                Undo
              </Button>
            ) : (
              <Button
                variant="outline"
                size="sm"
                className="h-7 text-xs"
                disabled={busy}
                onClick={handleMarkDone}
              >
                Mark done
              </Button>
            )}
          </div>
          {error ? (
            <p className="text-xs text-destructive mt-1">{error}</p>
          ) : null}
        </div>
        <span className="text-xs font-mono text-muted-foreground tabular-nums shrink-0 mt-0.5">
          {dueLabel(item)}
        </span>
      </div>
      {expanded && hasDetail ? (
        <div className="ml-[3.25rem] rounded-md border bg-muted/40 px-3 py-2 text-xs flex flex-col gap-1.5">
          {item.how_to ? (
            <p className="text-muted-foreground whitespace-pre-line">
              {item.how_to}
            </p>
          ) : null}
          {item.done_when ? (
            <p className="font-medium">
              <span className="text-muted-foreground">Done when: </span>
              {item.done_when}
            </p>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function pillToneForStatus(
  status: ActionItemStatus,
): NonNullable<StatusPillProps["tone"]> {
  switch (status) {
    case "OVERDUE":
      return "error";
    case "TODAY":
    case "DUE_SOON":
      return "warning";
    case "UPCOMING":
    default:
      return "accent";
  }
}

function dueLabel(item: ActionItem): string {
  if (item.status === "TODAY") return "today";
  const days = item.days_until;
  if (days === null) return item.dated ?? "—";
  if (days < 0) return `${Math.abs(days)}d ago`;
  if (days === 0) return "today";
  return `in ${days}d`;
}

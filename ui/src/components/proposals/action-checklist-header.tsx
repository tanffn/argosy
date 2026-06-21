"use client";

import { useEffect, useState } from "react";

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
 * the user's plan, framed in plain language with a count summary. It is
 * READ-ONLY — accept / reject affordances live on the proposal cards
 * below. Reuses the canonical action-items endpoint
 * (`api.planActionItems`), the same source the Home-page widget reads.
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

  useEffect(() => {
    // `loading` starts true; we only ever flip it false in finally. (Calling
    // setState synchronously in an effect body is forbidden by this Next.js's
    // react-hooks/set-state-in-effect lint rule.)
    let cancelled = false;
    api
      .planActionItems(userId, windowDays)
      .then((res) => {
        if (!cancelled) setData(res);
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

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">What&apos;s on you to do</CardTitle>
        <CardDescription>
          These are the moves only you can make right now. Review the details
          below, then act on the matching proposal cards.
        </CardDescription>
        {data ? (
          <div className="flex items-center gap-1.5 mt-1 flex-wrap">
            {data.overdue_count > 0 ? (
              <StatusPill tone="error" mono>
                {data.overdue_count} overdue
              </StatusPill>
            ) : null}
            {data.today_count > 0 ? (
              <StatusPill tone="warning" mono>
                {data.today_count} today
              </StatusPill>
            ) : null}
            <StatusPill tone="neutral" mono>
              {data.upcoming_count} upcoming
            </StatusPill>
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
              <ChecklistRow key={it.item_id} item={it} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

interface ChecklistRowProps {
  item: ActionItem;
}

function ChecklistRow({ item }: ChecklistRowProps) {
  const tone = pillToneForStatus(item.status);
  const sub = item.detail || item.rationale;

  return (
    <li className="flex items-start gap-3">
      <StatusPill tone={tone} mono className="mt-0.5">
        {item.status}
      </StatusPill>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold leading-snug">{item.label}</p>
        {sub ? (
          <p className="text-xs text-muted-foreground truncate">{sub}</p>
        ) : null}
      </div>
      <span className="text-xs font-mono text-muted-foreground tabular-nums shrink-0 mt-0.5">
        {dueLabel(item)}
      </span>
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

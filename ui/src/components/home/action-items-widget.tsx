"use client";

import { useCallback, useEffect, useState } from "react";

import { SectionHeader } from "@/components/ui/section-header";
import { StatusPill } from "@/components/ui/status-pill";
import { friendlyItemId, friendlySourceLabel } from "@/lib/plain-english-labels";
import {
  api,
  type ActionItem,
  type ActionItemsResponse,
  type ActionItemStatus,
} from "@/lib/api";

interface ActionItemsWidgetProps {
  userId: string;
  /** Days ahead of today to surface upcoming items. Defaults to 14. */
  windowDays?: number;
}

/**
 * Home-page widget surfacing the dated short/medium-horizon actions
 * from the user's pending draft (or current accepted plan).
 *
 * Renders three buckets stacked top-to-bottom: OVERDUE, TODAY/DUE_SOON,
 * and UPCOMING. Clicking any item toggles its detail/rationale and
 * cited sources inline.
 *
 * Read-only by design. Accepting / rejecting individual items still
 * flows through the existing /plan page draft-review surface.
 */
export function ActionItemsWidget({
  userId,
  windowDays = 14,
}: ActionItemsWidgetProps) {
  const [data, setData] = useState<ActionItemsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const res = await api.planActionItems(userId, windowDays);
      setData(res);
      setError(null);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [userId, windowDays]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount; refresh() sets local state from the API
    refresh();
  }, [refresh]);

  const items = data?.items ?? [];
  const overdue = items.filter((it) => it.status === "OVERDUE");
  const todayDueSoon = items.filter(
    (it) => it.status === "TODAY" || it.status === "DUE_SOON",
  );
  const upcoming = items.filter((it) => it.status === "UPCOMING");

  const total = items.length;
  const headerAction = (
    <div className="flex items-center gap-1.5">
      {data && data.overdue_count > 0 ? (
        <StatusPill tone="error" mono>
          OVERDUE {data.overdue_count}
        </StatusPill>
      ) : null}
      {data && data.today_count > 0 ? (
        <StatusPill tone="warning" mono>
          TODAY {data.today_count}
        </StatusPill>
      ) : null}
      {data ? (
        <StatusPill tone="neutral" mono>
          {total} in {windowDays}d
        </StatusPill>
      ) : null}
    </div>
  );

  return (
    <section>
      <SectionHeader label="ACTION ITEMS" action={headerAction} />
      <div className="rounded-lg border border-border bg-card px-4 py-3 flex flex-col gap-3">
        {loading ? (
          <p className="text-xs text-muted-foreground font-mono">loading…</p>
        ) : error ? (
          <p className="text-xs text-error font-mono">{error}</p>
        ) : total === 0 ? (
          <p className="text-xs text-muted-foreground font-mono">
            No dated actions in next {windowDays} days · plan synthesis pending
          </p>
        ) : (
          <>
            {overdue.length > 0 ? (
              <Bucket
                title={`OVERDUE (${overdue.length})`}
                dotClass="bg-error"
                titleClass="text-error"
                items={overdue}
              />
            ) : null}
            {todayDueSoon.length > 0 ? (
              <Bucket
                title={`TODAY / DUE SOON (${todayDueSoon.length})`}
                dotClass="bg-warning"
                titleClass="text-warning"
                items={todayDueSoon}
              />
            ) : null}
            {upcoming.length > 0 ? (
              <Bucket
                title={`UPCOMING (${upcoming.length})`}
                dotClass="bg-muted-foreground/60"
                titleClass="text-muted-foreground"
                items={upcoming}
              />
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}

interface BucketProps {
  title: string;
  dotClass: string;
  titleClass: string;
  items: ActionItem[];
}

function Bucket({ title, dotClass, titleClass, items }: BucketProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <div
        className={`flex items-center gap-2 font-mono text-[11px] uppercase tracking-wider ${titleClass}`}
      >
        <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${dotClass}`} />
        {title}
      </div>
      <ul className="flex flex-col gap-1 pl-3.5">
        {items.map((it) => (
          <ActionItemRow key={it.item_id} item={it} />
        ))}
      </ul>
    </div>
  );
}

interface ActionItemRowProps {
  item: ActionItem;
}

function ActionItemRow({ item }: ActionItemRowProps) {
  const [open, setOpen] = useState(false);

  const datedLabel = formatDateLabel(item);
  const horizonTone =
    item.horizon === "short" ? "accent" : ("neutral" as const);

  return (
    <li className="font-mono text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-start gap-2 text-left w-full hover:bg-secondary/30 rounded px-1.5 py-1 transition-colors"
        aria-expanded={open}
      >
        <span
          aria-hidden
          className="text-muted-foreground select-none mt-0.5"
        >
          {open ? "▾" : "▸"}
        </span>
        <span className={`tabular-nums ${dateClassForStatus(item.status)}`}>
          {datedLabel}
        </span>
        <span className="text-foreground flex-1 min-w-0">{item.label}</span>
        <StatusPill tone={horizonTone} mono>
          {item.horizon}
        </StatusPill>
      </button>
      {open ? (
        <div className="ml-6 mt-1 mb-1.5 flex flex-col gap-1.5 text-[11px] text-muted-foreground border-l border-border pl-3">
          {item.detail ? (
            <p className="whitespace-pre-wrap text-foreground/80">
              {item.detail}
            </p>
          ) : null}
          {item.rationale ? (
            <p className="whitespace-pre-wrap">
              <span className="text-muted-foreground/80">rationale:</span>{" "}
              {item.rationale}
            </p>
          ) : null}
          {item.cited_sources.length > 0 ? (
            <div className="flex flex-wrap gap-1 mt-0.5">
              {item.cited_sources.map((src, i) => (
                <span
                  key={`${item.item_id}-src-${i}`}
                  className="rounded-full border border-border bg-secondary/40 px-2 py-0.5 text-[10px]"
                  title={src}
                >
                  {friendlySourceLabel(src)}
                </span>
              ))}
            </div>
          ) : null}
          <div
            className="text-[10px] text-muted-foreground/70 tabular-nums"
            title={item.item_id}
          >
            plan #{item.plan_version_id} · item {friendlyItemId(item.item_id)}
          </div>
        </div>
      ) : null}
    </li>
  );
}

function dateClassForStatus(status: ActionItemStatus): string {
  switch (status) {
    case "OVERDUE":
      return "text-error";
    case "TODAY":
      return "text-warning font-semibold";
    case "DUE_SOON":
      return "text-warning";
    default:
      return "text-muted-foreground";
  }
}

function formatDateLabel(item: ActionItem): string {
  if (!item.dated) return "—";
  const days = item.days_until;
  if (item.status === "TODAY") return "Today";
  if (item.status === "OVERDUE" && days !== null) {
    const ago = Math.abs(days);
    return `${item.dated} (${ago}d ago)`;
  }
  if (item.status === "DUE_SOON" && days !== null) {
    return `${item.dated} (${days}d)`;
  }
  if (days !== null && days <= 14) {
    return `${item.dated} (${days}d)`;
  }
  return item.dated;
}

import type { InboxFeedDTO } from "@/lib/api";

/** A source error can leave an otherwise successful inbox response empty. */
export function hasInboxSourceError(feed: InboxFeedDTO) {
  return feed.dropped.some((row) => typeof row === "object" && row !== null && "reason" in row && row.reason === "source_error");
}

/** Stable presentation links, shared by the home preview and full inbox. */
export function inboxItemAnchor(id: string) {
  return `inbox-item-${id}`;
}

export function inboxItemHref(item: { id: string; kind: string }) {
  return item.kind === "order_sheet" ? "/inbox#trade-plan" : `/inbox#${encodeURIComponent(inboxItemAnchor(item.id))}`;
}

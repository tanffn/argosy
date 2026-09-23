/** Stable presentation links, shared by the home preview and full inbox. */
export function inboxItemAnchor(id: string) {
  return `inbox-item-${id}`;
}

export function inboxItemHref(item: { id: string; kind: string }) {
  return item.kind === "order_sheet" ? "/inbox#trade-plan" : `/inbox#${encodeURIComponent(inboxItemAnchor(item.id))}`;
}

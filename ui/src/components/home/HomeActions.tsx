"use client";

import Link from "next/link";
import { api, type InboxItemDTO } from "@/lib/api";
import { inboxItemHref } from "@/lib/inbox-presentation";
import { Card, CardContent } from "@/components/ui/card";
import { Markdown } from "@/components/markdown";
import { useHomeRead } from "./use-home-read";

const loadInbox = (userId: string, signal: AbortSignal) => api.getInbox(userId, false, signal);
const money = (value: number) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(value);

export function HomeActions({ userId }: { userId: string }) {
  const { data: feed, failed, reason, retry } = useHomeRead(loadInbox, userId);
  // Same buckets as Inbox: preserve server order, never rescore decisions here.
  const actions = feed?.items.filter((item) => (item.bucket ?? 99) < 6) ?? [];
  const observations = feed?.items.filter((item) => item.bucket === 6) ?? [];
  const incomplete = feed?.dropped.some((row) => typeof row === "object" && row !== null && "reason" in row && row.reason === "source_error");
  return (
    <Card data-slot="home-actions">
      <CardContent className="p-5 space-y-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-lg font-semibold">Your next actions</h2>
          <Link href="/inbox" className="text-sm text-info hover:underline">Open full inbox →</Link>
        </div>
        <p className="text-sm text-muted-foreground">The same ranked list as your Inbox. Review here; decide and record trades there.</p>
        {failed ? (
          <div role="alert" className="text-sm text-warning">
            The action list could not be refreshed. This does not mean there is nothing to do.
            <p className="mt-1">{reason} It will retry automatically.</p>
            <button onClick={retry} className="mt-2 inline-block underline">Retry now</button>
          </div>
        ) : !feed ? <p role="status" className="text-sm text-muted-foreground">Checking your next actions…</p> : (
          <>
            {feed.issues?.map((issue, index) => <p key={`${issue.code}:${index}`} role="alert" className="text-sm text-warning">{issue.message}</p>)}
            {incomplete && <p role="alert" className="text-sm text-warning">Some action sources failed. This list may be incomplete; check system status before acting.</p>}
            {feed.trade_plan?.approval_blocked && <p role="alert" className="text-sm text-warning">The current trade plan needs repair before approval. <Link href="/inbox#trade-plan" className="underline">See what is blocking it.</Link></p>}
            {actions.length ? (
              <>
                <ol className="space-y-3" aria-label="Prioritized next actions">
                  {actions.slice(0, 5).map((item, index) => <ActionRow key={item.id} item={item} number={index + 1} asOf={feed.generated_at} />)}
                </ol>
                {actions.length > 5 && <details className="rounded-md border border-border p-3">
                  <summary className="cursor-pointer text-sm">Show {actions.length - 5} more actions ({actions.length} total)</summary>
                  <ol start={6} className="mt-3 space-y-3">{actions.slice(5).map((item, index) => <ActionRow key={item.id} item={item} number={index + 6} asOf={feed.generated_at} />)}</ol>
                </details>}
              </>
            ) : <p className="text-sm">No decisions listed in the available inbox data. Check the analysis status above for coverage.</p>}
            {observations.length > 0 && <details className="border-t border-border pt-3">
              <summary className="cursor-pointer text-sm">On the radar · {observations.length} observations, not trade instructions</summary>
              <ul className="mt-2 space-y-2 text-sm">{observations.map((item) => <li key={item.id}><span>{item.title}</span><p className="text-muted-foreground">{item.why_now}</p></li>)}</ul>
            </details>}
            <p className="text-xs text-muted-foreground">List assembled: <time dateTime={feed.generated_at}>{feed.generated_at}</time>. This is not the age of its underlying evidence. Refreshes every minute and when you return.</p>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function ActionRow({ item, number, asOf }: { item: InboxItemDTO; number: number; asOf: string }) {
  // Expiration is an explicit record timestamp, not an investment judgment.
  const expired = item.expires_at != null && Date.parse(item.expires_at.endsWith("Z") || /[+-]\d{2}:\d{2}$/.test(item.expires_at) ? item.expires_at : `${item.expires_at}Z`) < Date.parse(asOf);
  const rationale = typeof item.body.rationale === "string" ? item.body.rationale : null;
  const orderLine = typeof item.body.order_line === "string" ? item.body.order_line : null;
  return (
    <li className="rounded-md border border-border p-3 space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-medium">{number}. {item.title}</h3>
        {item.amount_usd != null && Number.isFinite(item.amount_usd) && <span className="font-mono text-sm">{money(item.amount_usd)}</span>}
      </div>
      {orderLine && <p className="text-xs text-muted-foreground">Recorded order: {orderLine}</p>}
      {item.why_now && <p className="text-sm text-muted-foreground line-clamp-2">{item.why_now}</p>}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {item.due_at ? <span>Due: {item.due_at}</span> : <span>No deadline recorded</span>}
        {item.expires_at && <span className={expired ? "text-warning" : ""}>{expired ? "Expired — needs reassessment" : "Valid until"}: {item.expires_at}</span>}
      </div>
      <details>
        <summary className="cursor-pointer text-xs text-info">Why this action?</summary>
        <div className="mt-2 space-y-2 text-sm">
          {item.rank_reason && <p><strong>Why it is here:</strong> {item.rank_reason}</p>}
          {item.why_now && <p>{item.why_now}</p>}
          {rationale && <Markdown>{rationale}</Markdown>}
          {!rationale && !item.why_now && <p>No rationale supplied by this source.</p>}
          <Link href={inboxItemHref(item)} className="text-info underline">Full decision details →</Link>
        </div>
      </details>
      <Link href={inboxItemHref(item)} className="inline-block text-sm text-info hover:underline">{expired ? "Review expired proposal" : "Review action"} →</Link>
    </li>
  );
}

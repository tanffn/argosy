"use client";

/**
 * InboxItemCard — the one attention contract every inbox item shares.
 *
 * Envelope: a bucket chip, a plain-language title, the server-computed
 * rank_reason, a one-line why-now, a primary action + secondary actions, and an
 * expander for the typed body. The body differs per kind (trade / cash_deploy /
 * plan_task / note) but the envelope never does — so the user never has to
 * relearn how to act on a different kind of item.
 *
 * This component is presentational: it calls back to ``onAction(item, intent)``
 * and lets the page own the API calls + refresh. It never renders raw status
 * enums, account classes, tiers, ids, or API paths (the feed already strips
 * them; the client-copy eslint guard enforces it here too).
 */

import Link from "next/link";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type { InboxActionDTO, InboxItemDTO } from "@/lib/api";

function styleToVariant(style: InboxActionDTO["style"]) {
  if (style === "primary") return "default" as const;
  if (style === "danger") return "outline" as const;
  return "outline" as const;
}

function fmtUsd(n: number): string {
  return n.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  });
}

// --- per-kind bodies -------------------------------------------------------

function TradeBody({ body }: { body: Record<string, unknown> }) {
  const rationale = typeof body.rationale === "string" ? body.rationale : "";
  const orderLine = typeof body.order_line === "string" ? body.order_line : "";
  const instrument = typeof body.instrument === "string" ? body.instrument : "";
  const speculative = body.speculative === true;
  const conviction = typeof body.conviction === "string" ? body.conviction : null;
  return (
    <div className="space-y-2 text-sm">
      {(orderLine || instrument) && (
        <p className="text-xs text-muted-foreground">
          {orderLine}
          {orderLine && instrument ? " · " : ""}
          {instrument}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {speculative && <Badge variant="outline">Higher-risk idea</Badge>}
        {conviction && <Badge variant="secondary">{conviction} conviction</Badge>}
      </div>
      {rationale && <p>{rationale}</p>}
    </div>
  );
}

function CashDeployBody({ body }: { body: Record<string, unknown> }) {
  const headline = typeof body.headline === "string" ? body.headline : "";
  const buyList = Array.isArray(body.buy_list)
    ? (body.buy_list as Array<Record<string, unknown>>)
    : [];
  return (
    <div className="space-y-3 text-sm">
      {headline && <p>{headline}</p>}
      {buyList.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-muted-foreground text-left">
            <tr>
              <th className="py-1">Where it goes</th>
              <th className="py-1 text-right">Amount</th>
            </tr>
          </thead>
          <tbody>
            {buyList.map((b, i) => (
              <tr key={i} className="border-t border-border/30">
                <td className="py-1">
                  {String(b.instrument ?? b.asset_class ?? "—")}
                  {b.rationale ? (
                    <span className="text-muted-foreground"> — {String(b.rationale)}</span>
                  ) : null}
                </td>
                <td className="py-1 text-right font-mono">
                  {typeof b.amount_usd === "number" ? fmtUsd(b.amount_usd) : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Link
        href="/inbox#deploy-cash"
        className="text-primary hover:underline text-xs font-medium"
      >
        Open the full deploy-cash tool →
      </Link>
    </div>
  );
}

function PlanTaskBody({ body }: { body: Record<string, unknown> }) {
  const detail = typeof body.detail === "string" ? body.detail : "";
  const howTo = typeof body.how_to === "string" ? body.how_to : "";
  const doneWhen = typeof body.done_when === "string" ? body.done_when : "";
  return (
    <div className="space-y-2 text-sm">
      {detail && <p>{detail}</p>}
      {howTo && (
        <p>
          <span className="font-medium">How: </span>
          {howTo}
        </p>
      )}
      {doneWhen && (
        <p className="text-muted-foreground">
          <span className="font-medium">Done when: </span>
          {doneWhen}
        </p>
      )}
    </div>
  );
}

function NoteBody({ body }: { body: Record<string, unknown> }) {
  const detail = typeof body.detail === "string" ? body.detail : "";
  return detail ? <p className="text-sm">{detail}</p> : null;
}

function ItemBody({ item }: { item: InboxItemDTO }) {
  switch (item.kind) {
    case "trade":
    case "discovery_buy":
    case "switch":
      return <TradeBody body={item.body} />;
    case "cash_deploy":
      return <CashDeployBody body={item.body} />;
    case "plan_task":
      return <PlanTaskBody body={item.body} />;
    case "note":
      return <NoteBody body={item.body} />;
    default:
      return null;
  }
}

// --- the card --------------------------------------------------------------

interface Props {
  item: InboxItemDTO;
  busy: boolean;
  onAction: (item: InboxItemDTO, action: InboxActionDTO) => void;
}

export function InboxItemCard({ item, busy, onAction }: Props) {
  const [expanded, setExpanded] = useState(false);

  function fire(action: InboxActionDTO) {
    if (
      action.requires_confirmation &&
      !window.confirm(`${action.label}: are you sure?`)
    ) {
      return;
    }
    onAction(item, action);
  }

  return (
    <Card>
      <CardContent className="py-4 space-y-2">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              {item.bucket_label && (
                <Badge variant="outline" className="text-[11px]">
                  {item.bucket_label}
                </Badge>
              )}
              <h3 className="text-base font-medium truncate">{item.title}</h3>
            </div>
            {item.rank_reason && (
              <p className="text-xs text-muted-foreground mt-0.5">{item.rank_reason}</p>
            )}
          </div>
        </div>

        {item.why_now && <p className="text-sm">{item.why_now}</p>}

        {expanded && (
          <div className="border-t border-border/40 pt-3">
            <ItemBody item={item} />
          </div>
        )}

        <div className="flex items-center gap-2 flex-wrap pt-1">
          {item.primary_action && (
            <Button
              size="sm"
              variant={styleToVariant(item.primary_action.style)}
              disabled={busy}
              onClick={() => fire(item.primary_action!)}
            >
              {item.primary_action.label}
            </Button>
          )}
          {item.secondary_actions.map((a) => (
            <Button
              key={a.intent}
              size="sm"
              variant={styleToVariant(a.style)}
              disabled={busy}
              onClick={() => fire(a)}
            >
              {a.label}
            </Button>
          ))}
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-xs text-muted-foreground hover:text-foreground ml-auto"
          >
            {expanded ? "Hide details" : "Details"}
          </button>
        </div>
      </CardContent>
    </Card>
  );
}

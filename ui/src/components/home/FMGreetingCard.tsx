"use client";

/**
 * FMGreetingCard — the Fund Manager's first greeting, the FIRST and
 * dominant surface on the home page.
 *
 * Renders GET /api/home/greeting verbatim (server-side assembly; no
 * client-side triage):
 *
 *   Good morning, Ariel.
 *   The book: $4.00M · on plan · FI track: 2028 (age 46)
 *   ► I need one thing from you: …   [Do it] [Show me why]
 *   ► Worth your attention (2): …    (explicit "no action needed")
 *   Everything else is quiet. Next scheduled review: 17:00.
 *   [Ask me anything] [Full detail →]
 *
 * "Why" is one click away: each needs-you item expands its why_md
 * inline. [Full detail →] asks the page to expand the demoted plumbing
 * region (banners, tiles, system health) — the greeting itself never
 * shows plumbing.
 */

import Link from "next/link";
import { useEffect, useState } from "react";

import { LiveClock } from "@/components/live-clock";
import { Card, CardContent } from "@/components/ui/card";
import { StatusPill } from "@/components/ui/status-pill";
import { api, type GreetingDTO, type GreetingNeedsYouItemDTO } from "@/lib/api";

interface Props {
  userId: string;
  /** Called when the client clicks [Full detail →]. */
  onShowFullDetail?: () => void;
}

/**
 * Greeting header — the small brand mark, the salutation, and the live
 * clock right-aligned. This IS the top of the page now (the old brand
 * hero was redundant with the nav), so the logo + clock live here.
 */
function GreetingHeader({ name }: { name?: string }) {
  const hello = salutation(new Date().getHours());
  return (
    <div
      className="flex items-center justify-between gap-3"
      data-testid="greeting-header"
    >
      <div className="flex items-center gap-2.5 min-w-0">
        {/* eslint-disable-next-line @next/next/no-img-element -- static brand mark */}
        <img
          src="/logo.png"
          alt=""
          className="h-6 w-6 shrink-0 rounded-md"
          aria-hidden
        />
        <p className="font-mono text-xl font-semibold">
          {hello}
          {name ? `, ${name}` : ""}.
        </p>
      </div>
      <LiveClock className="text-base shrink-0" />
    </div>
  );
}

/** Local-time salutation — the FM greets like a human would. */
export function salutation(hour: number): string {
  if (hour >= 5 && hour < 12) return "Good morning";
  if (hour >= 12 && hour < 18) return "Good afternoon";
  return "Good evening";
}

/** Clean, size-proportional book figure — never cent precision. */
export function formatBookUsd(totalUsd: number | null): string {
  if (totalUsd === null || !Number.isFinite(totalUsd)) return "—";
  if (Math.abs(totalUsd) >= 1_000_000)
    return `$${(totalUsd / 1_000_000).toFixed(2)}M`;
  return `$${Math.round(totalUsd / 1_000).toLocaleString()}K`;
}

export function FMGreetingCard({ userId, onShowFullDetail }: Props) {
  const [greeting, setGreeting] = useState<GreetingDTO | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .homeGreeting(userId)
      .then((g) => {
        if (!cancelled) setGreeting(g);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  if (failed) {
    return (
      <Card className="border-l-2 border-l-warning/60" data-slot="fm-greeting">
        <CardContent className="px-5 py-4">
          <GreetingHeader />
          <p className="text-xs text-muted-foreground mt-1">
            The desk is unreachable right now — this card will recover on
            the next load.
          </p>
        </CardContent>
      </Card>
    );
  }

  if (!greeting) {
    return (
      <Card className="border-l-2 border-l-success/60" data-slot="fm-greeting">
        <CardContent className="px-5 py-4">
          <GreetingHeader />
          <p className="text-xs text-muted-foreground mt-1">…</p>
        </CardContent>
      </Card>
    );
  }

  const needsCount = greeting.needs_you.length;
  const needsHeader =
    needsCount === 1
      ? "I need one thing from you:"
      : `I need ${needsCount} things from you:`;

  return (
    <Card className="border-l-2 border-l-success/60" data-slot="fm-greeting">
      <CardContent className="px-5 py-4 flex flex-col gap-4">
        {/* Salutation header (logo + clock) + the book line */}
        <div className="flex flex-col gap-1">
          <GreetingHeader name={greeting.greeting_name} />
          <p
            className="font-mono text-sm tabular-nums"
            data-testid="book-line"
            title={greeting.book.on_plan_note}
          >
            The book: {formatBookUsd(greeting.book.total_usd)}
            {" · "}
            <span
              className={
                greeting.book.on_plan ? "text-success" : "text-warning"
              }
            >
              {greeting.book.on_plan ? "on plan" : "in transition"}
            </span>
            {" · "}
            {greeting.book.fi_line}
          </p>
          {!greeting.book.on_plan && greeting.book.on_plan_note ? (
            <p className="text-[11px] text-muted-foreground font-mono">
              {greeting.book.on_plan_note}
            </p>
          ) : null}
        </div>

        {/* ► I need … from you */}
        {needsCount > 0 ? (
          <div className="flex flex-col gap-2" data-testid="needs-you">
            <p className="font-mono text-sm font-semibold">
              <span aria-hidden className="text-warning">
                ►{" "}
              </span>
              {needsHeader}
            </p>
            <ul className="flex flex-col gap-2">
              {greeting.needs_you.map((item) => (
                <NeedsYouRow key={item.id} item={item} />
              ))}
            </ul>
          </div>
        ) : null}

        {/* ► Worth your attention */}
        {greeting.watching.length > 0 ? (
          <div className="flex flex-col gap-2" data-testid="watching">
            <p className="font-mono text-sm font-semibold">
              <span aria-hidden className="text-info">
                ►{" "}
              </span>
              Worth your attention ({greeting.watching.length}):
            </p>
            <ul className="flex flex-col gap-1.5">
              {greeting.watching.map((w) => (
                <li
                  key={w.id}
                  className="rounded-md border border-border bg-secondary/30 px-3 py-2"
                >
                  <p className="text-xs font-mono text-foreground">
                    {w.headline}
                  </p>
                  <p className="text-[11px] text-muted-foreground mt-0.5">
                    {w.note}
                  </p>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {/* Quiet line + next review */}
        <p
          className="text-xs text-muted-foreground font-mono"
          data-testid="quiet-line"
        >
          {greeting.quiet
            ? "Everything is quiet — nothing needs you."
            : "Everything else is quiet."}
          {greeting.next_review_local
            ? ` Next scheduled review: ${greeting.next_review_local}.`
            : ""}
        </p>

        {/* Options row */}
        <div className="flex items-center gap-3 flex-wrap border-t border-border pt-3">
          <Link
            href="/consult"
            className="font-mono text-xs text-info hover:underline"
          >
            Ask me anything
          </Link>
          <button
            type="button"
            onClick={onShowFullDetail}
            className="font-mono text-xs text-muted-foreground hover:text-foreground hover:underline"
            data-testid="full-detail-btn"
          >
            Full detail →
          </button>
        </div>
      </CardContent>
    </Card>
  );
}

function NeedsYouRow({ item }: { item: GreetingNeedsYouItemDTO }) {
  const [showWhy, setShowWhy] = useState(false);
  // Generic rendering — the backend adds needs_you kinds additively
  // (verified / needs-confirm action items), so both affordances are
  // conditional: an item with a cta gets the [label] link, an item with
  // a non-empty why_md gets the expandable "Show me why".
  const hasWhy =
    typeof item.why_md === "string" && item.why_md.trim().length > 0;
  const cta = item.cta ?? null;
  return (
    <li className="rounded-md border border-border bg-secondary/30 px-3 py-2 flex flex-col gap-1.5">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <span className="text-xs font-mono text-foreground min-w-0">
          {item.headline}
        </span>
        <StatusPill tone="warning" mono>
          needs you
        </StatusPill>
      </div>
      {cta || hasWhy ? (
        <div className="flex items-center gap-3 flex-wrap">
          {cta ? (
            <Link
              href={cta.href}
              className="font-mono text-xs text-info hover:underline"
              data-testid={`cta-${item.id}`}
            >
              {cta.label} →
            </Link>
          ) : null}
          {hasWhy ? (
            <button
              type="button"
              onClick={() => setShowWhy((v) => !v)}
              aria-expanded={showWhy}
              className="font-mono text-[11px] text-muted-foreground hover:text-foreground hover:underline"
              data-testid={`why-toggle-${item.id}`}
            >
              {showWhy ? "Hide why" : "Show me why"}
            </button>
          ) : null}
        </div>
      ) : null}
      {showWhy && hasWhy ? (
        <pre
          className="whitespace-pre-wrap text-[11px] font-mono text-muted-foreground bg-background/60 rounded-md px-3 py-2 mt-1"
          data-testid={`why-${item.id}`}
        >
          {item.why_md}
        </pre>
      ) : null}
    </li>
  );
}

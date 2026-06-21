"use client";

import {
  AlertTriangle,
  FileCheck,
  Headphones,
  RadioTower,
  TrendingUp,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type ComponentType, type SVGProps } from "react";

import { StatusPill } from "@/components/ui/status-pill";
import {
  api,
  type AdvisorBriefBulletKind,
  type AdvisorHomeBriefResponse,
} from "@/lib/api";
import { cn } from "@/lib/utils";

interface AdvisorBriefCardProps {
  userId: string;
  className?: string;
}

const KIND_META: Record<
  AdvisorBriefBulletKind,
  {
    Icon: ComponentType<SVGProps<SVGSVGElement>>;
    iconClass: string;
    label: string;
  }
> = {
  draft_plan: {
    Icon: FileCheck,
    iconClass: "text-error",
    label: "draft",
  },
  gap: {
    Icon: AlertTriangle,
    iconClass: "text-warning",
    label: "gap",
  },
  portfolio: {
    Icon: TrendingUp,
    iconClass: "text-success",
    label: "portfolio",
  },
  signal: {
    Icon: RadioTower,
    iconClass: "text-info",
    label: "signal",
  },
};

/** Human-readable relative time from an ISO timestamp ("2m ago", "1h ago"). */
function relativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "just now";
  const diffMs = Date.now() - t;
  const sec = Math.max(0, Math.floor(diffMs / 1000));
  if (sec < 60) return "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}

/**
 * Glance card for the home page that surfaces the Advisor as a
 * front-and-center presence: a headline, 3–5 stitched bullets, and a
 * "Talk to advisor" CTA. Fetches `/api/advisor/home-brief` directly
 * (NOT through the Next.js `rewrites()` proxy — see api.ts for why).
 *
 * Glass-card aesthetic mirrors the brand-hero on the same page.
 */
export function AdvisorBriefCard({ userId, className }: AdvisorBriefCardProps) {
  const [data, setData] = useState<AdvisorHomeBriefResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    // Kick off the fetch, then funnel state into the React tree from
    // the resolution callback only — never synchronously inside the
    // effect body. Avoids the `react-hooks/set-state-in-effect` lint.
    api
      .advisorHomeBrief(userId)
      .then((res) => {
        if (cancelled) return;
        setData(res);
        setError(null);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        // Always log the raw error for dev visibility — but render a
        // friendly, fixed-string message in the UI so users don't see
        // stack traces / "Error: HTTP 500" in their home page.
        console.error("advisor-brief-card: fetch failed", e);
        // AbortError (from the 8s fetch timeout below) gets a more
        // specific copy so the user knows it's a connectivity issue,
        // not a stale-data issue.
        const isAbort =
          e instanceof DOMException && e.name === "AbortError";
        setError(
          isAbort
            ? "Couldn't reach advisor service."
            : "Brief will refresh shortly.",
        );
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  // While fetching, render a placeholder card with the headline slot
  // taken so the page doesn't shift when the data lands.
  return (
    <section
      className={cn(
        // Single-accent treatment matching the brand-hero pattern:
        // the prior top-edge gradient stripe was one of three
        // competing atmospherics in the first 500px of viewport
        // (Plan/Codex ideation pass flagged it). Replaced with a
        // left-edge rule in the info semantic token — echoes the
        // advisor identity without competing with the brand-hero
        // green left rule above.
        "relative rounded-lg overflow-hidden bg-card/80 backdrop-blur-sm border border-border border-l-2 border-l-info/60",
        className,
      )}
      data-slot="advisor-brief-card"
      aria-label="Advisor brief"
    >
      <div className="relative px-5 py-4 flex flex-col gap-3">
        {/* Header row: avatar/icon + headline + CTA */}
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3 min-w-0">
            <span
              className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-info/30 bg-info/10 text-info"
              aria-hidden
            >
              <Headphones className="h-4 w-4" suppressHydrationWarning />
            </span>
            <div className="min-w-0">
              <h2 className="font-mono font-semibold text-sm leading-tight">
                {data?.headline ??
                  (loading ? "Loading your brief…" : "Advisor")}
              </h2>
              <p className="text-[11px] text-muted-foreground mt-0.5">
                Stitched from your latest gaps, daily brief, and watchlist.
              </p>
            </div>
          </div>
          <Link
            href={data?.cta.href ?? "/advisor"}
            className="shrink-0 inline-flex items-center gap-1.5 rounded-md border border-info/40 bg-info/10 hover:bg-info/20 text-info px-3 py-1.5 text-xs font-mono transition-colors duration-200"
          >
            {data?.cta.label ?? "Talk to advisor"}
            <span aria-hidden>→</span>
          </Link>
        </div>

        {/* Bullet list */}
        {error ? (
          <div className="text-xs text-error font-mono">{error}</div>
        ) : data && data.bullets.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {data.bullets.map((b, i) => {
              const meta = KIND_META[b.kind];
              const Icon = meta.Icon;
              return (
                <li
                  key={i}
                  className="flex items-start gap-2.5 text-sm leading-snug"
                >
                  <Icon
                    className={cn("h-4 w-4 shrink-0 mt-0.5", meta.iconClass)}
                    aria-hidden
                    suppressHydrationWarning
                  />
                  <span className="min-w-0">
                    <StatusPill
                      tone={
                        b.kind === "draft_plan"
                          ? "error"
                          : b.kind === "gap"
                            ? "warning"
                            : b.kind === "portfolio"
                              ? "success"
                              : "accent"
                      }
                      mono
                      className="mr-2"
                    >
                      {meta.label}
                    </StatusPill>
                    {b.text}
                  </span>
                </li>
              );
            })}
          </ul>
        ) : data && data.bullets.length === 0 ? (
          <div className="text-xs text-muted-foreground font-mono">
            All caught up. Nothing to surface right now.
          </div>
        ) : (
          // Loading placeholder — three faint skeleton rows so the card's
          // height matches the eventual layout and the page doesn't jump.
          <ul className="flex flex-col gap-1.5" aria-hidden>
            {[0, 1, 2].map((i) => (
              <li key={i} className="flex items-center gap-2.5">
                <span className="h-4 w-4 rounded-sm bg-muted/40" />
                <span className="h-3 w-2/3 rounded bg-muted/30" />
              </li>
            ))}
          </ul>
        )}

        {/* Footer micro-copy */}
        {data?.generated_at ? (
          <div className="text-[10px] text-muted-foreground font-mono tabular-nums">
            Updated {relativeTime(data.generated_at)}
          </div>
        ) : null}
      </div>
    </section>
  );
}

"use client";

/**
 * PlanStoryLead — the plain-language plan "story" rendered as the LEAD section
 * of /retirement (the standalone /overview tab was merged in here). It fetches
 * the same canonical /api/overview payload and renders each chapter via the
 * shared <ChapterPanel>, preceded by a heading and (when relevant) the
 * "things are waiting for you" actions banner.
 *
 * Loading model — THREE explicit states tracked by `status`:
 *   - "loading": the fetch is in flight → spinner + "Loading your plan story…"
 *   - "error":   the fetch threw, OR the payload reports available===false →
 *                a friendly card (never a perpetual spinner, never fake data)
 *   - "ready":   chapters render
 * We deliberately do NOT treat `data===null` as "loading" — that was the bug
 * that left the page spinning forever on a failed/404 fetch.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { Loader2 } from "lucide-react";

import { api, type OverviewResponse } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ChapterPanel } from "@/components/overview/ChapterPanel";

const USER_ID = "ariel";

type Status = "loading" | "error" | "ready";

export function PlanStoryLead() {
  const [status, setStatus] = useState<Status>("loading");
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  // Friendly message for the error/unavailable state (fetch error text or the
  // payload's `reason`); null while loading / when ready.
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .overview(USER_ID)
      .then((d) => {
        if (cancelled) return;
        if (!d.available) {
          setMessage(
            d.reason ||
              "No current plan yet — once a plan is promoted, your plain-language overview shows up here.",
          );
          setStatus("error");
          return;
        }
        setOverview(d);
        setStatus("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setMessage(
          "Couldn't load your plan story — the overview service didn't respond. Try refreshing in a moment.",
        );
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const heading = (
    <h2 className="text-lg font-semibold text-foreground">
      Your plan, in plain words
    </h2>
  );

  if (status === "loading") {
    return (
      <section className="space-y-3">
        {heading}
        <Card>
          <CardContent className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            Loading your plan story…
          </CardContent>
        </Card>
      </section>
    );
  }

  if (status === "error") {
    return (
      <section className="space-y-3">
        {heading}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Couldn&apos;t load your plan story</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">{message}</p>
          </CardContent>
        </Card>
      </section>
    );
  }

  const chapters = overview?.chapters ?? [];
  const banner = overview?.actions_banner;

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        {heading}
        {chapters.length > 1 ? (
          <nav className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
            {chapters.map((c, i) => (
              <a
                key={c.id}
                href={`#chapter-${c.id}`}
                className="transition-colors hover:text-foreground"
              >
                <span className="font-mono text-[10px]">{i + 1}.</span> {c.title}
              </a>
            ))}
          </nav>
        ) : null}
      </div>

      {/* Actions banner — only when there are open, user-owned actions. */}
      {banner && banner.open_count > 0 ? (
        <Link
          href={banner.href}
          className="flex items-center justify-between rounded-lg border border-warning/30 bg-warning/10 px-4 py-3 text-sm font-medium text-warning transition-colors hover:bg-warning/20"
        >
          <span>
            {banner.open_count}{" "}
            {banner.open_count === 1 ? "thing is" : "things are"} waiting for you
          </span>
          <span aria-hidden>→</span>
        </Link>
      ) : null}

      {chapters.length === 0 ? (
        <Card>
          <CardContent className="py-6 text-sm text-muted-foreground">
            No chapters to show yet.
          </CardContent>
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {chapters.map((c) => (
            // registerRef is a no-op here: scroll-spy lives in the standalone
            // page; inside the tab we use plain #chapter-<id> anchors instead.
            <ChapterPanel key={c.id} chapter={c} registerRef={() => {}} />
          ))}
        </div>
      )}
    </section>
  );
}

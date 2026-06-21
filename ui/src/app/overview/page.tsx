"use client";

/**
 * Overview — the plain-language plan-explainer surface (Layout C).
 *
 * A two-column page: a sticky left chapter rail (click-to-focus + scroll-spy
 * highlight) and a focused story panel on the right. Every number on the page
 * is resolver-derived and arrives pre-rendered from /api/overview — this page
 * carries zero hardcoded financial magic numbers. When the payload is null or
 * unavailable we render placeholders / a friendly note, never invented data.
 *
 * Design spec: docs/superpowers/specs/2026-06-21-overview-plan-explainer-design.md
 * Mirrors the fetch + null-handling pattern of /retirement.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import { api, type OverviewResponse } from "@/lib/api";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ChapterPanel } from "@/components/overview/ChapterPanel";
import { ChapterRail } from "@/components/overview/ChapterRail";

const USER_ID = "ariel";

export default function OverviewPage() {
  // Single canonical payload. Null until the fetch resolves; a null payload
  // keeps the page in its placeholder state rather than rendering fake data.
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  // `activeId` is null until the user scrolls/clicks; we fall back to the
  // first chapter for the rail highlight (derived, not stored — avoids a
  // setState-in-effect cascade).
  const [activeId, setActiveId] = useState<string | null>(null);

  // chapter id -> section element, for scroll-spy + click-to-focus scroll.
  const sectionRefs = useRef<Map<string, HTMLElement>>(new Map());

  useEffect(() => {
    let cancelled = false;
    api
      .overview(USER_ID)
      .then((d) => {
        if (!cancelled) setOverview(d);
      })
      .catch(() => {
        // Leave `overview` null — the page stays in its placeholder state
        // instead of rendering invented numbers.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const chapters = useMemo(
    () => (overview?.available ? overview.chapters : []),
    [overview],
  );

  // Effective rail highlight: the spied/clicked chapter, else the first.
  const effectiveActiveId =
    activeId ?? (chapters.length > 0 ? chapters[0].id : null);

  // Scroll-spy: highlight the chapter whose section is most in view.
  useEffect(() => {
    if (chapters.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        // Pick the most-visible intersecting section.
        let best: { id: string; ratio: number } | null = null;
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          const id = (e.target as HTMLElement).dataset.chapterId;
          if (!id) continue;
          if (best == null || e.intersectionRatio > best.ratio) {
            best = { id, ratio: e.intersectionRatio };
          }
        }
        if (best) setActiveId(best.id);
      },
      { rootMargin: "-20% 0px -55% 0px", threshold: [0.1, 0.25, 0.5, 0.75] },
    );
    const els = sectionRefs.current;
    els.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [chapters]);

  const registerRef = (id: string, el: HTMLElement | null) => {
    if (el) sectionRefs.current.set(id, el);
    else sectionRefs.current.delete(id);
  };

  const handleSelect = (id: string) => {
    setActiveId(id);
    const el = sectionRefs.current.get(id);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  // --- Unavailable / loading states (no spinners; friendly cards). ---
  if (overview != null && !overview.available) {
    return (
      <div className="container mx-auto max-w-3xl px-4 py-6">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Overview</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              {overview.reason ||
                "No current plan yet — once a plan is promoted, your plain-language overview shows up here."}
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="container mx-auto grid max-w-6xl grid-cols-1 gap-6 px-4 py-6 lg:grid-cols-[220px_1fr]">
      <ChapterRail
        chapters={chapters}
        activeId={effectiveActiveId}
        onSelect={handleSelect}
      />

      <div className="flex min-w-0 flex-col gap-4">
        {/* Actions banner — only when there are open, user-owned actions. */}
        {overview?.actions_banner &&
        overview.actions_banner.open_count > 0 ? (
          <Link
            href={overview.actions_banner.href}
            className="flex items-center justify-between rounded-lg border border-warning/30 bg-warning/10 px-4 py-3 text-sm font-medium text-warning transition-colors hover:bg-warning/20"
          >
            <span>
              {overview.actions_banner.open_count}{" "}
              {overview.actions_banner.open_count === 1 ? "thing is" : "things are"}{" "}
              waiting for you
            </span>
            <span aria-hidden>→</span>
          </Link>
        ) : null}

        {overview == null ? (
          // Loading placeholder (mirrors retirement's quiet loading affordance).
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Overview</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Loading your plan story…
              </p>
            </CardContent>
          </Card>
        ) : chapters.length === 0 ? (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Overview</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                No chapters to show yet.
              </p>
            </CardContent>
          </Card>
        ) : (
          chapters.map((c) => (
            <ChapterPanel key={c.id} chapter={c} registerRef={registerRef} />
          ))
        )}
      </div>
    </div>
  );
}

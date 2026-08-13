"use client";

/**
 * /overview — plain-language plan-explainer surface.
 *
 * Layout C (spec §3.1): sticky left chapter rail + focused story panel on the
 * right. Scroll-spy highlights the active chapter in the rail; clicking a rail
 * entry smooth-scrolls to that chapter.
 *
 * Every number shown is resolver-derived (no hardcoded figures). If the backend
 * marks `available=false` (no current plan), we show a friendly unavailable
 * state rather than fabricating content.
 */

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { ChapterPanel } from "@/components/overview/ChapterPanel";
import { ChapterRail } from "@/components/overview/ChapterRail";
import type { OverviewChapter, OverviewResponse } from "@/lib/api";
import { api } from "@/lib/api";

const USER_ID = "ariel";

export default function OverviewPage() {
  const [data, setData] = useState<OverviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);

  // Map chapter id → DOM element, for scroll-spy and click-to-scroll.
  const chapterRefsRef = useRef<Map<string, HTMLElement>>(new Map());

  const registerRef = useCallback(
    (id: string, el: HTMLElement | null) => {
      if (el) {
        chapterRefsRef.current.set(id, el);
      } else {
        chapterRefsRef.current.delete(id);
      }
    },
    [],
  );

  // Fetch on mount. All setState calls are inside promise callbacks so
  // the lint rule react-hooks/set-state-in-effect is not triggered.
  useEffect(() => {
    let cancelled = false;
    api
      .overview(USER_ID)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setLoading(false);
          if (d.chapters.length > 0) {
            setActiveId(d.chapters[0].id);
          }
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Scroll-spy: update activeId as the user scrolls.
  useEffect(() => {
    if (!data || data.chapters.length === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        // Pick the topmost visible chapter.
        const visible = entries
          .filter((e) => e.isIntersecting)
          .map((e) => e.target.getAttribute("data-chapter-id"))
          .filter(Boolean) as string[];
        if (visible.length > 0) {
          setActiveId(visible[0]);
        }
      },
      { rootMargin: "-20% 0px -60% 0px", threshold: 0 },
    );

    for (const el of chapterRefsRef.current.values()) {
      observer.observe(el);
    }
    return () => observer.disconnect();
  }, [data]);

  const handleRailSelect = useCallback((id: string) => {
    const el = chapterRefsRef.current.get(id);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      setActiveId(id);
    }
  }, []);

  const chapters: OverviewChapter[] = data?.chapters ?? [];

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
          <p className="text-sm text-muted-foreground">
            {data?.as_of
              ? `Plan story · as of ${data.as_of}`
              : "Plain-language plan story"}
          </p>
        </div>
      </header>

      {error && (
        <p className="text-sm text-error font-mono">{error}</p>
      )}

      {loading && (
        <p className="text-sm text-muted-foreground">Loading…</p>
      )}

      {!loading && data && !data.available && (
        <div className="rounded-md border border-border/60 bg-muted/20 p-6 text-sm text-muted-foreground">
          <p className="font-medium text-foreground">No plan available yet.</p>
          {data.reason && <p className="mt-1">{data.reason}</p>}
          <p className="mt-3">
            Run{" "}
            <code className="font-mono">argosy ingest plan &lt;path&gt;</code>{" "}
            to import a plan, or click{" "}
            <Link href="/plan" className="text-primary hover:underline">
              Plan
            </Link>{" "}
            to generate a draft.
          </p>
        </div>
      )}

      {!loading && data?.available && (
        <>
          {/* Actions banner */}
          {data.actions_banner.open_count > 0 && (
            <Link
              href={data.actions_banner.href}
              className="flex items-center gap-2 rounded-md border border-warning/30 bg-warning/10 px-4 py-2.5 text-sm font-medium text-warning transition-colors hover:bg-warning/20"
            >
              <span aria-hidden>▸</span>
              {data.actions_banner.open_count} thing
              {data.actions_banner.open_count === 1 ? "" : "s"} waiting for
              you →
            </Link>
          )}

          {/* Layout C: rail + story panel */}
          <div className="flex gap-8 items-start">
            {/* Left rail — sticky, hidden on small screens */}
            <aside className="w-48 shrink-0">
              <ChapterRail
                chapters={chapters}
                activeId={activeId}
                onSelect={handleRailSelect}
              />
            </aside>

            {/* Right story panel — chapters in sequence */}
            <div className="flex-1 flex flex-col gap-6 min-w-0">
              {chapters.map((chapter) => (
                <ChapterPanel
                  key={chapter.id}
                  chapter={chapter}
                  registerRef={registerRef}
                />
              ))}
            </div>
          </div>
        </>
      )}
    </main>
  );
}

"use client";

/**
 * ChapterPanel — one chapter's full story card: eyebrow, big title, the
 * plain-language headline (rendered as-is; already translated by the
 * backend), the viz, a drill link to the matching expert surface, and an
 * optional "YOUR MOVE" chip deep-linking to /proposals.
 *
 * If the chapter is degraded, a subtle muted note replaces failure — never
 * fabricate a number.
 */

import Link from "next/link";

import { Card, CardContent } from "@/components/ui/card";
import type { OverviewChapter } from "@/lib/api";

import { ChapterViz } from "./ChapterViz";

interface ChapterPanelProps {
  chapter: OverviewChapter;
  registerRef: (id: string, el: HTMLElement | null) => void;
}

export function ChapterPanel({ chapter, registerRef }: ChapterPanelProps) {
  return (
    <section
      id={`chapter-${chapter.id}`}
      ref={(el) => registerRef(chapter.id, el)}
      data-chapter-id={chapter.id}
      className="scroll-mt-6"
    >
      <Card>
        <CardContent className="flex flex-col gap-5">
          <header className="flex flex-col gap-2">
            {chapter.eyebrow ? (
              <span className="text-[11px] font-mono uppercase tracking-[0.14em] text-muted-foreground">
                {chapter.eyebrow}
              </span>
            ) : null}
            <h2 className="text-xl font-semibold leading-tight text-foreground sm:text-2xl">
              {chapter.title}
            </h2>
            {chapter.headline ? (
              <p className="text-sm leading-relaxed text-muted-foreground sm:text-base">
                {chapter.headline}
              </p>
            ) : null}
          </header>

          <div className="rounded-lg border border-border/60 bg-secondary/20 p-4">
            <ChapterViz viz={chapter.viz} />
          </div>

          {chapter.degraded ? (
            <p className="text-xs italic text-muted-foreground">
              Some figures aren&apos;t computed yet — showing what&apos;s
              available.
            </p>
          ) : null}

          <div className="flex flex-wrap items-center gap-3">
            {chapter.drill_href ? (
              <Link
                href={chapter.drill_href}
                className="text-xs font-medium text-info transition-colors hover:text-info/80"
              >
                {chapter.drill_label || "See the detail"} →
              </Link>
            ) : null}

            {chapter.your_move ? (
              <Link
                href={chapter.your_move.href}
                className="inline-flex items-center gap-1.5 rounded-full border border-warning/30 bg-warning/10 px-3 py-1 text-xs font-medium text-warning transition-colors hover:bg-warning/20"
              >
                <span aria-hidden>▸</span>
                <span className="font-mono uppercase tracking-wider">
                  Your move
                </span>
                <span className="font-normal normal-case tracking-normal">
                  {chapter.your_move.label}
                </span>
                <span aria-hidden>→</span>
              </Link>
            ) : null}
          </div>
        </CardContent>
      </Card>
    </section>
  );
}

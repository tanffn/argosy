"use client";

/**
 * ChapterRail — the sticky left rail listing each chapter's title. Clicking a
 * row smooth-scrolls to / focuses that chapter; the active chapter (driven by
 * scroll-spy in the page) is highlighted. Pure presentational; the page owns
 * the active id + the scroll handler.
 */

import { cn } from "@/lib/utils";
import type { OverviewChapter } from "@/lib/api";

interface ChapterRailProps {
  chapters: OverviewChapter[];
  activeId: string | null;
  onSelect: (id: string) => void;
}

export function ChapterRail({
  chapters,
  activeId,
  onSelect,
}: ChapterRailProps) {
  return (
    <nav className="hidden lg:block sticky top-6 self-start text-sm">
      <div className="mb-2 text-[10px] font-mono uppercase tracking-wider text-muted-foreground">
        The story
      </div>
      <ol className="space-y-1.5">
        {chapters.map((c, i) => {
          const active = c.id === activeId;
          return (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => onSelect(c.id)}
                aria-current={active ? "true" : undefined}
                className={cn(
                  "flex w-full items-baseline gap-2 border-l-2 pl-2 -ml-0.5 text-left transition-colors",
                  active
                    ? "border-foreground text-foreground font-medium"
                    : "border-transparent text-muted-foreground hover:border-foreground/40 hover:text-foreground",
                )}
              >
                <span className="font-mono text-[10px] text-muted-foreground">
                  {i + 1}
                </span>
                <span className="leading-snug">{c.title}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

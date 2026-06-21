"use client";

/**
 * ChapterViz — dispatches a chapter's viz payload to the right component by
 * `viz.kind`. The backend types `viz.data` as Record<string, unknown>, so we
 * cast to each component's typed shape at the boundary here (one place).
 */

import type { OverviewVizPayload } from "@/lib/api";

import { AllocVsTarget, type AllocVsTargetData } from "./AllocVsTarget";
import { DualTrackAge, type DualTrackAgeData } from "./DualTrackAge";
import { FiCrossingHero, type FiCrossingData } from "./FiCrossingHero";
import { LiquidSplit, type LiquidSplitData } from "./LiquidSplit";
import { NvdaWinddown, type NvdaWinddownData } from "./NvdaWinddown";
import { PhaseTimeline, type PhaseTimelineData } from "./PhaseTimeline";
import { RsuForward, type RsuForwardData } from "./RsuForward";

export function ChapterViz({ viz }: { viz: OverviewVizPayload }) {
  const data = (viz?.data ?? {}) as Record<string, unknown>;

  switch (viz?.kind) {
    case "fi_crossing":
      return <FiCrossingHero data={data as unknown as FiCrossingData} />;
    case "liquid_split":
      return <LiquidSplit data={data as unknown as LiquidSplitData} />;
    case "alloc_vs_target":
      return <AllocVsTarget data={data as unknown as AllocVsTargetData} />;
    case "nvda_winddown":
      return <NvdaWinddown data={data as unknown as NvdaWinddownData} />;
    case "rsu_forward":
      return <RsuForward data={data as unknown as RsuForwardData} />;
    case "phase_timeline":
      return <PhaseTimeline data={data as unknown as PhaseTimelineData} />;
    case "dual_track_age":
      return <DualTrackAge data={data as unknown as DualTrackAgeData} />;
    default:
      return (
        <p className="py-6 text-center text-sm text-muted-foreground">
          No visual for this chapter.
        </p>
      );
  }
}

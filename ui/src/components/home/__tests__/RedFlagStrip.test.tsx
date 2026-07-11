import { describe, expect, it } from "vitest";

import { buildSummary } from "@/components/home/RedFlagStrip";
import type { MonitorFlagDTO } from "@/lib/api";


describe("buildSummary", () => {
  it("uses generic monitor payload copy without per-kind UI logic", () => {
    const flag: MonitorFlagDTO = {
      id: 84,
      kind: "signal_stream_warning",
      severity: "warning",
      payload: {
        headline: "SCHW: insider cluster warning",
        summary: "SCHW generated a short insider cluster signal for monitoring.",
        detail: "Two C-suite sellers reduced verified stakes.",
        ticker: "SCHW",
        source_urls: [
          "https://www.sec.gov/Archives/edgar/data/1/form4.xml",
        ],
      },
      surfaced_at: "2026-07-11T09:30:00Z",
      expires_at: "2026-08-10T09:30:00Z",
      acknowledged_at: null,
    };

    expect(buildSummary(flag)).toBe(
      "SCHW generated a short insider cluster signal for monitoring.",
    );
  });
});

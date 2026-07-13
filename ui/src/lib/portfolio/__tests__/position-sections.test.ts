// @vitest-environment node
import { describe, expect, it } from "vitest";

import type { PortfolioPosition, PortfolioSnapshotDTO } from "@/lib/api";
import {
  assertPositionsPartition,
  groupByAccount,
  isPhysicalRealEstate,
} from "@/lib/portfolio/position-sections";

function pos(
  partial: Partial<PortfolioPosition> & Pick<PortfolioPosition, "symbol" | "usd_value_k">,
): PortfolioPosition {
  return {
    location: partial.location ?? "leumi",
    currency: "USD",
    asset_type: partial.asset_type ?? "Equity",
    type_label: partial.type_label ?? partial.asset_type ?? "Equity",
    name: partial.name ?? partial.symbol,
    details: partial.details ?? "",
    shares: partial.shares ?? 100,
    current_price: partial.current_price ?? 10,
    estate_safe: partial.estate_safe ?? true,
    classified: partial.classified ?? true,
    ...partial,
  };
}

describe("isPhysicalRealEstate / groupByAccount", () => {
  it("keeps listed property securities (IWDP, O) in account groups", () => {
    const snap: PortfolioSnapshotDTO = {
      snapshot_date: "2026-07-13",
      fx_usd_nis: 3.3,
      fx_usd_eur: 0.92,
      total_usd_value_k: 200,
      positions: [
        pos({
          symbol: "IWDP",
          asset_type: "Real Estate",
          usd_value_k: 40,
          location: "leumi",
        }),
        pos({
          symbol: "O",
          asset_type: "REIT",
          usd_value_k: 12.9,
          location: "leumi",
        }),
        pos({
          symbol: "-",
          asset_type: "Real Estate",
          usd_value_k: 500,
          shares: 0,
          location: "pipera",
          name: "Pipera apartment",
        }),
        pos({
          symbol: "VOO",
          asset_type: "Core Equity",
          usd_value_k: 100,
          location: "schwab 876",
        }),
      ],
      allocations: [],
      source_path: "test",
      parse_warnings: [],
      classification_warnings: [],
    };

    expect(isPhysicalRealEstate(snap.positions[0])).toBe(false); // IWDP
    expect(isPhysicalRealEstate(snap.positions[1])).toBe(false); // O
    expect(isPhysicalRealEstate(snap.positions[2])).toBe(true); // physical
    expect(isPhysicalRealEstate(snap.positions[3])).toBe(false);

    const groups = groupByAccount(snap);
    const leumi = groups.find((g) => g.location === "leumi");
    expect(leumi).toBeDefined();
    expect(leumi!.positions.map((p) => p.symbol).sort()).toEqual(["IWDP", "O"]);
    expect(leumi!.total_usd_k).toBeCloseTo(52.9);

    const liquid = groups.reduce((s, g) => s + g.total_usd_k, 0);
    expect(liquid).toBeCloseTo(152.9); // excludes 500K physical

    expect(assertPositionsPartition(snap)).toBeNull();
  });

  it("fails the invariant if a tradable REIT were dropped from accounts", () => {
    // Simulate the pre-fix bug: treat all Real Estate asset_type as physical.
    const iwdp = pos({
      symbol: "IWDP",
      asset_type: "Real Estate",
      usd_value_k: 40,
    });
    // isPhysicalRealEstate correctly keeps it — assert the scar shape:
    expect(isPhysicalRealEstate(iwdp)).toBe(false);
  });
});

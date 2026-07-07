import { describe, expect, it } from "vitest";

import { formatPlanLabel, shortPlanLabel } from "../plan-label";

describe("shortPlanLabel", () => {
  it("strips timestamp suffixes and pipeline words", () => {
    expect(shortPlanLabel("x10-sleeve-draft-20260706-124710")).toBe(
      "x10 sleeve",
    );
    expect(shortPlanLabel("refinement-draft-2026-07-06-052139")).toBe(
      "refinement",
    );
    expect(shortPlanLabel("allocation-rebuild")).toBe("allocation rebuild");
  });
});

describe("formatPlanLabel", () => {
  it("composes Plan v<N> · <short label> · <Mon YYYY>", () => {
    expect(
      formatPlanLabel({
        plan_version_id: 67,
        version_label: "x10-sleeve-draft-20260706-124710",
        imported_at: "2026-07-06T12:47:13.895309+00:00",
      }),
    ).toBe("Plan v67 · x10 sleeve · Jul 2026");
  });

  it("degrades gracefully when parts are missing", () => {
    expect(
      formatPlanLabel({
        plan_version_id: null,
        version_label: "refinement-draft-2026-07-06-052139",
        imported_at: null,
      }),
    ).toBe("refinement");
    expect(formatPlanLabel(null)).toBeNull();
    expect(
      formatPlanLabel({
        plan_version_id: null,
        version_label: null,
        imported_at: null,
      }),
    ).toBeNull();
  });
});

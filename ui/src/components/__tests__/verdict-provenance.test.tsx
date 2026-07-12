import { describe, expect, it } from "vitest";

import {
  formatCheckedAgo,
  formatValidationDue,
  provenanceFromBody,
} from "@/components/verdict-provenance";

describe("verdict provenance helpers", () => {
  const now = Date.parse("2026-07-12T12:00:00Z");

  it("formats checked-ago relatively", () => {
    expect(formatCheckedAgo("2026-07-12T11:30:00Z", now)).toBe("30m ago");
    expect(formatCheckedAgo("2026-07-10T12:00:00Z", now)).toBe("2d ago");
    expect(formatCheckedAgo(null, now)).toBeNull();
  });

  it("formats next-validation due relative to today", () => {
    expect(formatValidationDue("2026-07-12", now)).toBe("due today");
    expect(formatValidationDue("2026-07-15", now)).toBe("due in 3d");
    expect(formatValidationDue("2026-07-10", now)).toBe("overdue 2d");
  });

  it("extracts nested provenance from inbox body", () => {
    const p = provenanceFromBody({
      rationale: "x",
      provenance: {
        falsifier_state: "none_recorded",
        falsifiers: [],
        next_validation: null,
        last_fleet_check_at: "2026-07-10T12:00:00+00:00",
      },
    });
    expect(p?.falsifier_state).toBe("none_recorded");
    expect(p?.last_fleet_check_at).toContain("2026-07-10");
  });
});

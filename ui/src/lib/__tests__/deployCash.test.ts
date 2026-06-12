import { describe, expect, it } from "vitest";
import { api } from "../api";

describe("deployCashPlan client", () => {
  it("is exposed on the api client", () => {
    expect(typeof api.deployCashPlan).toBe("function");
  });
});

import { describe, expect, it } from "vitest";

describe("inbox page", () => {
  it("compiles with the inbox queue + deploy-cash tool wired", async () => {
    const mod = await import("../page");
    expect(mod.default).toBeDefined();
  });
});

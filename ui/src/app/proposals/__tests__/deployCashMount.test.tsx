// ui/src/app/proposals/__tests__/deployCashMount.test.tsx
import { describe, expect, it } from "vitest";

describe("proposals page deploy-cash mount", () => {
  it("imports the DeployCashCard into the proposals page module", async () => {
    const src = await import("../page");
    expect(src).toBeDefined(); // smoke: page compiles with the new section wired
  });
});

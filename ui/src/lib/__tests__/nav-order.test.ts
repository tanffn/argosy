import { describe, expect, it } from "vitest";

import { PRIMARY_TABS } from "@/components/nav";

describe("primary nav order", () => {
  it("places Proposals immediately after Portfolio", () => {
    const hrefs = PRIMARY_TABS.map((t) => t.href);
    const portfolio = hrefs.indexOf("/portfolio");
    const proposals = hrefs.indexOf("/proposals");
    expect(portfolio).toBeGreaterThanOrEqual(0);
    expect(proposals).toBe(portfolio + 1);
  });
});

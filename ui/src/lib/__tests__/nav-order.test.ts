import { describe, expect, it } from "vitest";

import { PRIMARY_TABS } from "@/components/nav";

describe("primary nav order", () => {
  it("places Inbox immediately after Portfolio", () => {
    const hrefs = PRIMARY_TABS.map((t) => t.href);
    const portfolio = hrefs.indexOf("/portfolio");
    const inbox = hrefs.indexOf("/inbox");
    expect(portfolio).toBeGreaterThanOrEqual(0);
    expect(inbox).toBe(portfolio + 1);
  });
});

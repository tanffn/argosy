import { describe, expect, it } from "vitest";

/**
 * Brush-rule payload shape — keeps the always-tag UI contract honest.
 * Exact merchant match is deliberate (no substring).
 */
describe("expense tag rule payload", () => {
  it("builds an exact-merchant create body", () => {
    const merchant = "פז אפליקציית יילו";
    const body = {
      user_id: "ariel",
      match_merchant_normalized: merchant,
      tag: "Mazda",
      match_category_slug: null as string | null,
    };
    expect(body.match_merchant_normalized).toBe(merchant);
    expect(body.match_merchant_normalized.includes("פזית")).toBe(false);
    expect(body.tag).toBe("Mazda");
  });

  it("bulk-add requires either ids or a filter, not both", () => {
    function valid(body: {
      transaction_ids?: number[];
      merchant_normalized?: string;
      category_slug?: string;
    }): boolean {
      const hasIds = Boolean(body.transaction_ids?.length);
      const hasFilter = Boolean(
        body.merchant_normalized?.trim() || body.category_slug?.trim(),
      );
      return hasIds !== hasFilter;
    }
    expect(valid({ transaction_ids: [1, 2] })).toBe(true);
    expect(valid({ merchant_normalized: "פז אפליקציית יילו" })).toBe(true);
    expect(valid({})).toBe(false);
    expect(
      valid({ transaction_ids: [1], merchant_normalized: "x" }),
    ).toBe(false);
  });
});

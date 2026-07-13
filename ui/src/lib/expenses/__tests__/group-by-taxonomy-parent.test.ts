// @vitest-environment node
import { describe, expect, it } from "vitest";

import type { CategorySpend } from "@/lib/expenses/api";
import {
  groupByTaxonomyParent,
  parentRowKey,
} from "@/lib/expenses/group-by-taxonomy-parent";

function cat(
  partial: Partial<CategorySpend> & Pick<CategorySpend, "slug" | "total_nis">,
): CategorySpend {
  return {
    label_en: partial.label_en ?? partial.slug,
    transaction_count: partial.transaction_count ?? 1,
    percent: partial.percent ?? 0,
    parent_slug: partial.parent_slug ?? null,
    parent_label: partial.parent_label ?? null,
    ...partial,
  };
}

describe("groupByTaxonomyParent", () => {
  it("clusters 2+ leaves under a synthetic parent with a non-colliding key", () => {
    const cats = [
      cat({
        slug: "transportation.fuel",
        label_en: "Fuel",
        total_nis: 400,
        percent: 40,
        parent_slug: "transportation",
        parent_label: "Car",
      }),
      cat({
        slug: "transportation.parking",
        label_en: "Parking",
        total_nis: 100,
        percent: 10,
        parent_slug: "transportation",
        parent_label: "Car",
      }),
    ];
    const out = groupByTaxonomyParent(cats, 500);
    expect(out.map((r) => r.key)).toEqual([
      parentRowKey("transportation"),
      "transportation.fuel",
      "transportation.parking",
    ]);
    expect(out[0]).toMatchObject({
      slug: null,
      label_en: "Car",
      total_nis: 500,
      percent: 50,
      depth: 0,
    });
    expect(out[1].parentKey).toBe(parentRowKey("transportation"));
  });

  it("emits an Unspecified child for parent-level spend (totals reconcile)", () => {
    const cats = [
      cat({
        slug: "transportation",
        label_en: "Car",
        total_nis: 50,
        percent: 5,
        parent_slug: null,
      }),
      cat({
        slug: "transportation.fuel",
        label_en: "Fuel",
        total_nis: 400,
        percent: 40,
        parent_slug: "transportation",
        parent_label: "Car",
      }),
      cat({
        slug: "transportation.parking",
        label_en: "Parking",
        total_nis: 100,
        percent: 10,
        parent_slug: "transportation",
        parent_label: "Car",
      }),
    ];
    const out = groupByTaxonomyParent(cats, 550);
    const keys = out.map((r) => r.key);
    expect(new Set(keys).size).toBe(keys.length);
    expect(keys).not.toContain("transportation");
    expect(keys[0]).toBe(parentRowKey("transportation"));
    expect(out[0].total_nis).toBe(550);
    expect(out[0].percent).toBe(55);
    expect(out[0].transaction_count).toBe(3);

    const children = out.filter((r) => r.depth === 1);
    expect(children.map((r) => r.slug).sort()).toEqual([
      "transportation",
      "transportation.fuel",
      "transportation.parking",
    ]);
    const unspecified = children.find((r) => r.label_en === "Unspecified");
    expect(unspecified).toMatchObject({
      key: "direct:transportation",
      slug: "transportation",
      total_nis: 50,
      percent: 5,
    });
    expect(children.reduce((s, c) => s + c.total_nis, 0)).toBe(out[0].total_nis);
    expect(children.reduce((s, c) => s + c.percent, 0)).toBe(out[0].percent);
  });

  it("promotes a single leaf + parent-level spend into a collapsible group", () => {
    const cats = [
      cat({
        slug: "travel",
        label_en: "Vacation",
        total_nis: 200,
        percent: 20,
      }),
      cat({
        slug: "travel.flights",
        label_en: "Flights",
        total_nis: 800,
        percent: 80,
        parent_slug: "travel",
        parent_label: "Vacation",
      }),
    ];
    const out = groupByTaxonomyParent(cats, 1000);
    expect(out[0].key).toBe(parentRowKey("travel"));
    expect(out[0].total_nis).toBe(1000);
    expect(out[0].percent).toBe(100);
    const children = out.filter((r) => r.depth === 1);
    expect(children).toHaveLength(2);
    expect(children.some((r) => r.label_en === "Unspecified")).toBe(true);
  });

  it("keeps a lone leaf flat when the parent has no own spend", () => {
    const cats = [
      cat({
        slug: "food.groceries",
        label_en: "Groceries",
        total_nis: 300,
        parent_slug: "food",
        parent_label: "Food",
      }),
    ];
    const out = groupByTaxonomyParent(cats, 300);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      key: "food.groceries",
      slug: "food.groceries",
      depth: 0,
    });
  });
});

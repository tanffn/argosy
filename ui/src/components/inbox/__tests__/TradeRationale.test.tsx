import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { parseRationale, TradeRationale } from "../TradeRationale";

// A verbatim-shaped legacy rationale (proposal 2, NOW): one dense paragraph,
// inline section labels, bracket citations with report ids + raw URLs.
const LEGACY = [
  "Verdict: redeploy (do not adopt into a sleeve). Quality read: ServiceNow is",
  "genuinely a strong business — very little debt and ~22% revenue growth",
  "[fundamentals/NOW]. Price read: but the price already bakes in years of that",
  "growth; the bear won on exactly this point",
  "[fundamentals/NOW; debate: fundamentals/NOW, https://finnhub.io/api/news?id=abc123].",
  "Thesis-fit read (the decisive point): this is US-large-cap AI/enterprise",
  "software — it overlaps the book's NVDA concentration",
  "[domain_knowledge/tax/us/estate_tax_nonresidents.md].",
  "Recommendation: sell in a single tranche and route the proceeds to NVDA.",
].join(" ");

// The structured markdown form the trader emits going forward.
const MARKDOWN = [
  "**Verdict:** Sell.",
  "",
  "**Quality read:** A strong business, but priced for perfection.",
  "",
  "**Price read:** Only ~16% to fair value — no buffer.",
  "",
  "**Recommendation:** Sell in one tranche.",
  "",
  "**Sources:** fundamentals/NOW, https://finnhub.io/api/news?id=abc123",
].join("\n");

// A second live variant (proposals 5/7): ALL-CAPS improvised labels and
// PARENTHESIZED citations instead of brackets.
const ALL_CAPS = [
  "VERDICT: REDEPLOY (sell and reinvest), staged — not a bare hold.",
  "QUALITY: Berkshire is a genuinely strong, low-debt company —",
  "debt-to-equity (D/E) of 0.18",
  "(fundamentals/BRK.B). PRICE: our fair-value estimate sits about 0.7%",
  "ABOVE the top of the 52-week range (fundamentals/BRK.B).",
  "TRIGGER TO PAUSE: a drawdown beyond 15% pauses the glide.",
].join(" ");

const UNSTRUCTURED =
  "This proposal has no labeled parts at all. It still needs to read as " +
  "paragraphs, not one blob. The company is fine. The price is not. We " +
  "recommend patience until the next earnings report lands.";

describe("parseRationale", () => {
  it("splits the legacy inline-label paragraph into labeled sections", () => {
    const parsed = parseRationale(LEGACY);
    const labels = parsed.sections.map((s) => s.label);
    expect(labels).toEqual([
      "Verdict",
      "Quality read",
      "Price read",
      "Thesis fit",
      "Recommendation",
    ]);
    expect(parsed.sections[0].text).toContain("redeploy");
    // Citations are lifted OUT of the prose…
    for (const s of parsed.sections) {
      expect(s.text).not.toContain("[fundamentals");
      expect(s.text).not.toContain("http");
    }
    // …and into the sources list, deduped, with the debate: prefix stripped.
    expect(parsed.sources).toContain("fundamentals/NOW");
    expect(parsed.sources).toContain("https://finnhub.io/api/news?id=abc123");
    expect(parsed.sources).toContain(
      "domain_knowledge/tax/us/estate_tax_nonresidents.md",
    );
    expect(
      parsed.sources.filter((s) => s === "fundamentals/NOW"),
    ).toHaveLength(1);
  });

  it("parses the markdown section form and folds the Sources line", () => {
    const parsed = parseRationale(MARKDOWN);
    const labels = parsed.sections.map((s) => s.label);
    expect(labels).toEqual([
      "Verdict",
      "Quality read",
      "Price read",
      "Recommendation",
    ]);
    // No leftover markdown bold markers in the rendered text.
    for (const s of parsed.sections) expect(s.text).not.toContain("**");
    expect(parsed.sources).toEqual([
      "https://finnhub.io/api/news?id=abc123",
      "fundamentals/NOW",
    ]);
  });

  it("handles ALL-CAPS improvised labels and parenthesized citations", () => {
    const parsed = parseRationale(ALL_CAPS);
    expect(parsed.sections.map((s) => s.label)).toEqual([
      "Verdict",
      "Quality read",
      "Price read",
      "Trigger to pause",
    ]);
    // Parenthesized report-id citations are lifted; ordinary parentheticals
    // ("(sell and reinvest)") stay in the prose.
    expect(parsed.sources).toEqual(["fundamentals/BRK.B"]);
    expect(parsed.sections[0].text).toContain("(sell and reinvest)");
    expect(parsed.sections[1].text).not.toContain("fundamentals/BRK.B");
    // Financial abbreviations are NOT citations — they stay in the prose.
    expect(parsed.sections[1].text).toContain("(D/E)");
  });

  it("falls back to sentence clusters — never a single blob", () => {
    const parsed = parseRationale(UNSTRUCTURED);
    expect(parsed.sections.length).toBeGreaterThan(1);
    expect(parsed.sections.every((s) => s.label === null)).toBe(true);
    expect(parsed.sections.map((s) => s.text).join(" ")).toContain(
      "recommend patience",
    );
  });

  it("keeps non-citation brackets in the prose", () => {
    const parsed = parseRationale("The filing says growth is 'durable' [sic]. More text follows here.");
    expect(parsed.sources).toEqual([]);
    expect(parsed.sections.map((s) => s.text).join(" ")).toContain("[sic]");
  });

  it("handles empty input", () => {
    expect(parseRationale("")).toEqual({ sections: [], sources: [] });
  });
});

describe("TradeRationale", () => {
  it("renders labeled paragraphs and a Sources row with URL links", () => {
    render(<TradeRationale text={LEGACY} />);
    expect(screen.getByText("Verdict:")).toBeInTheDocument();
    expect(screen.getByText("Recommendation:")).toBeInTheDocument();
    expect(screen.getByText("Sources")).toBeInTheDocument();
    // Raw URL never appears as text — it becomes a hostname-labeled link.
    const link = screen.getByRole("link", { name: "finnhub.io" });
    expect(link).toHaveAttribute(
      "href",
      "https://finnhub.io/api/news?id=abc123",
    );
    expect(screen.getByText("fundamentals/NOW")).toBeInTheDocument();
  });

  it("renders nothing for empty text", () => {
    const { container } = render(<TradeRationale text="" />);
    expect(container).toBeEmptyDOMElement();
  });
});

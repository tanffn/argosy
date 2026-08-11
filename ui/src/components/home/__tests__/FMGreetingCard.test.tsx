import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GreetingDTO } from "@/lib/api";

// Mock the api singleton so the card renders a fixture greeting.
const homeGreeting = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      homeGreeting: (...args: unknown[]) => homeGreeting(...args),
    },
  };
});

import {
  FMGreetingCard,
  formatBookUsd,
  salutation,
} from "../FMGreetingCard";

const GREETING: GreetingDTO = {
  greeting_name: "Ariel",
  book: {
    total_usd: 3999279.0,
    on_plan: false,
    on_plan_note:
      "transition in progress — biggest gap: Individual Stocks (non-NVDA, to redeploy) 18.4% vs 0.0% target",
    fi_line: "FI track: 2028 (age 46)",
  },
  needs_you: [
    {
      id: "proposal:47",
      kind: "allocate",
      headline: "Deploy ~$98k idle cash: EXUS $19k, CSPX $18k …",
      why_md: "Your idle cash sits above the plan-target threshold.",
      cta: { label: "Open the deploy tool", href: "/inbox#deploy-cash" },
    },
  ],
  watching: [
    {
      id: "flag:86",
      headline: "A USD cash account flipped negative alongside an ~83% drawdown",
      note: "No action needed — resolves with the next broker export.",
    },
    {
      id: "flag:85",
      headline: "NKE thesis weakened: sustained fundamental deterioration",
      note: "No action needed — the team is monitoring.",
    },
  ],
  quiet: false,
  next_review_local: "17:00",
};

const QUIET_GREETING: GreetingDTO = {
  greeting_name: "Ariel",
  book: {
    total_usd: 4000000,
    on_plan: true,
    on_plan_note: "all classes within ±5pp of target (ex-NVDA glide)",
    fi_line: "FI track: 2028 (age 46)",
  },
  needs_you: [],
  watching: [],
  quiet: true,
  next_review_local: "17:00",
};

describe("FMGreetingCard", () => {
  it("renders the greeting header: logo, time-of-day salutation, live clock", async () => {
    homeGreeting.mockResolvedValueOnce(GREETING);
    render(<FMGreetingCard userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByTestId("book-line")).toBeInTheDocument(),
    );
    const header = screen.getByTestId("greeting-header");
    // Salutation by local time of day, addressed to the greeting_name.
    const expected = salutation(new Date().getHours());
    expect(header.textContent).toContain(`${expected}, Ariel.`);
    // Small inline brand mark (the old hero panel is gone).
    const logo = header.querySelector("img");
    expect(logo).not.toBeNull();
    expect(logo!.getAttribute("src")).toBe("/logo.png");
    // Live clock placeholder/value right in the header (HH:MM:SS ticks
    // in after mount; the SSR-safe placeholder is --:--:--).
    expect(header.textContent).toMatch(/(\d{2}:\d{2}:\d{2}|--:--:--)/);
  });

  it("renders the header even while loading and on failure", async () => {
    // Loading: unresolved promise.
    homeGreeting.mockReturnValueOnce(new Promise(() => {}));
    const { unmount } = render(<FMGreetingCard userId="ariel" />);
    expect(screen.getByTestId("greeting-header")).toBeInTheDocument();
    unmount();

    // Failure: rejected fetch — header + recovery copy.
    homeGreeting.mockRejectedValueOnce(new Error("down"));
    render(<FMGreetingCard userId="ariel" />);
    await waitFor(() =>
      expect(
        screen.getByText(/The desk is unreachable right now/),
      ).toBeInTheDocument(),
    );
    expect(screen.getByTestId("greeting-header")).toBeInTheDocument();
  });

  it("renders a needs-you item without cta/why_md generically", async () => {
    homeGreeting.mockResolvedValueOnce({
      ...GREETING,
      needs_you: [
        {
          id: "action:9",
          kind: "needs_confirm",
          headline: "Confirm the SGOV cover-sale settled",
          // Backend may omit these for new kinds — the row must still
          // render the headline and simply skip both affordances.
          why_md: null,
          cta: null,
        },
      ],
    });
    render(<FMGreetingCard userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByTestId("needs-you")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("Confirm the SGOV cover-sale settled"),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("cta-action:9")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("why-toggle-action:9"),
    ).not.toBeInTheDocument();
  });

  it("renders the book line, needs-you item and CTA, and expands why", async () => {
    homeGreeting.mockResolvedValueOnce(GREETING);
    render(<FMGreetingCard userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByTestId("book-line")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("book-line").textContent).toContain("$4.00M");
    expect(screen.getByTestId("book-line").textContent).toContain(
      "FI track: 2028",
    );
    expect(screen.getByTestId("book-line").textContent).toContain(
      "in transition",
    );

    // needs-you: header + headline + CTA link to the deploy tool.
    expect(screen.getByText("I need one thing from you:")).toBeInTheDocument();
    expect(
      screen.getByText(/Deploy ~\$98k idle cash/),
    ).toBeInTheDocument();
    const cta = screen.getByTestId("cta-proposal:47");
    expect(cta).toHaveAttribute("href", "/inbox#deploy-cash");
    expect(cta.textContent).toContain("Open the deploy tool");

    // "Show me why" expands the rationale, one click away.
    expect(screen.queryByTestId("why-proposal:47")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("why-toggle-proposal:47"));
    expect(screen.getByTestId("why-proposal:47").textContent).toContain(
      "idle cash sits above the plan-target threshold",
    );
  });

  it("renders watching lines with their explicit no-action notes", async () => {
    homeGreeting.mockResolvedValueOnce(GREETING);
    render(<FMGreetingCard userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByTestId("watching")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("Worth your attention (2):"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No action needed — resolves with the next broker export."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No action needed — the team is monitoring."),
    ).toBeInTheDocument();
  });

  it("renders the quiet state with the next scheduled review", async () => {
    homeGreeting.mockResolvedValueOnce(QUIET_GREETING);
    render(<FMGreetingCard userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByTestId("quiet-line")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("quiet-line").textContent).toContain(
      "Everything is quiet — nothing needs you.",
    );
    expect(screen.getByTestId("quiet-line").textContent).toContain(
      "Next scheduled review: 17:00.",
    );
    expect(screen.queryByTestId("needs-you")).not.toBeInTheDocument();
    expect(screen.queryByTestId("watching")).not.toBeInTheDocument();
    expect(screen.getByTestId("book-line").textContent).toContain("on plan");
  });

  it("fires onShowFullDetail from the options row", async () => {
    homeGreeting.mockResolvedValueOnce(QUIET_GREETING);
    const onShow = vi.fn();
    render(<FMGreetingCard userId="ariel" onShowFullDetail={onShow} />);

    await waitFor(() =>
      expect(screen.getByTestId("full-detail-btn")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("full-detail-btn"));
    expect(onShow).toHaveBeenCalledTimes(1);

    // [Ask me anything] routes to the consult surface.
    expect(screen.getByText("Ask me anything")).toHaveAttribute(
      "href",
      "/consult",
    );
  });
});

describe("how our calls did", () => {
  it("renders win and miss outcomes with the right grade cue and headline", async () => {
    homeGreeting.mockResolvedValueOnce({
      ...QUIET_GREETING,
      how_our_calls_did: [
        {
          id: "verdict:12",
          subject: "NVDA",
          verdict: "SELL NVDA",
          grade: "win",
          move_pct: -6.0,
          headline: "SELL NVDA (Aug 8): NVDA -6% since — good call",
          as_of: "2026-08-10",
        },
        {
          id: "verdict:11",
          subject: "META",
          verdict: "HOLD META",
          grade: "miss",
          move_pct: 8.0,
          headline: "HOLD META (Jul 20): META +8% since — miss — worth revisiting",
          as_of: "2026-08-05",
        },
      ],
    });
    render(<FMGreetingCard userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByTestId("calls-did")).toBeInTheDocument(),
    );
    // Section heading
    expect(screen.getByText("How our calls did:")).toBeInTheDocument();
    // Both headlines are rendered
    expect(
      screen.getByText(/SELL NVDA.*good call/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/HOLD META.*worth revisiting/),
    ).toBeInTheDocument();
    // Grade dots carry the right aria-label
    expect(screen.getByLabelText("win")).toBeInTheDocument();
    expect(screen.getByLabelText("miss")).toBeInTheDocument();
  });

  it("renders nothing when how_our_calls_did is empty", async () => {
    homeGreeting.mockResolvedValueOnce({
      ...QUIET_GREETING,
      how_our_calls_did: [],
    });
    render(<FMGreetingCard userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByTestId("quiet-line")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("calls-did")).not.toBeInTheDocument();
  });

  it("renders nothing when how_our_calls_did is absent", async () => {
    homeGreeting.mockResolvedValueOnce(QUIET_GREETING);
    render(<FMGreetingCard userId="ariel" />);

    await waitFor(() =>
      expect(screen.getByTestId("quiet-line")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("calls-did")).not.toBeInTheDocument();
  });
});

describe("helpers", () => {
  it("salutation follows local time of day", () => {
    expect(salutation(8)).toBe("Good morning");
    expect(salutation(13)).toBe("Good afternoon");
    expect(salutation(21)).toBe("Good evening");
    expect(salutation(2)).toBe("Good evening");
  });

  it("formatBookUsd renders clean size-proportional figures", () => {
    expect(formatBookUsd(3999279)).toBe("$4.00M");
    expect(formatBookUsd(412345)).toBe("$412K");
    expect(formatBookUsd(null)).toBe("—");
  });
});

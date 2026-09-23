import { render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { FMGreetingCard, formatBookUsd } from "../FMGreetingCard";
import { HomeActions } from "../HomeActions";

/** Opt-in integration: real API helpers, live backend/database, real components.
 * No mocked network or agent seam; does not approve orders or launch a fleet. */
it.skipIf(process.env.ARGOSY_UI_LIVE !== "1")("renders the live family summary and the real ranked inbox", async () => {
  // Model the real UI origin, so short reads exercise Next's proxy as well.
  vi.spyOn(window, "location", "get").mockReturnValue(new URL("http://127.0.0.1:1337") as unknown as Location);
  const [greeting, inbox] = await Promise.all([api.homeGreeting("ariel"), api.getInbox("ariel", true)]);
  render(<><FMGreetingCard userId="ariel" summaryOnly /><HomeActions userId="ariel" /></>);
  await screen.findByTestId("family-summary", {}, { timeout: 25000 });
  expect(screen.getByText(formatBookUsd(greeting.book.total_usd))).toBeInTheDocument();
  if (greeting.book.as_of) expect(screen.getByText(`Snapshot dated ${greeting.book.as_of}`, { exact: false })).toBeInTheDocument();
  const first = inbox.items.find((item) => (item.bucket ?? 99) < 6);
  await waitFor(() => {
    if (first) expect(screen.getByText(`1. ${first.title}`)).toBeInTheDocument();
    else expect(screen.getByText(/No decisions listed in the available inbox data/)).toBeInTheDocument();
  }, { timeout: 25000 });
  expect(screen.queryByTestId("needs-you")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /execute|approve/i })).not.toBeInTheDocument();
  console.info(JSON.stringify({ live_api: true, snapshot_date: greeting.book.as_of, inbox_generated_at: inbox.generated_at, ranked_items: inbox.items.length, first_action_id: first?.id, trade_plan_present: inbox.trade_plan != null }));
}, 60000);

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { InboxFeedDTO, InboxItemDTO } from "@/lib/api";
import { inboxItemAnchor } from "@/lib/inbox-presentation";

const getInbox = vi.fn();
vi.mock("@/lib/api", () => ({ api: { getInbox: (...args: unknown[]) => getInbox(...args) } }));
import { HomeActions } from "../HomeActions";

const item = (id: string, extra: Partial<InboxItemDTO> = {}): InboxItemDTO => ({
  id, kind: "trade", title: `Review ${id}`, why_now: "A dated catalyst changed the thesis.",
  rank_reason: "Time-sensitive review", bucket: 1, bucket_label: "Overdue or expiring",
  primary_action: { intent: "execute", label: "Execute now", style: "primary", requires_confirmation: true },
  secondary_actions: [], body: {}, due_at: "2026-09-12", expires_at: null, amount_usd: 10000,
  source_refs: [], trace: null, ...extra,
});
const feed = (items: InboxItemDTO[], extra: Partial<InboxFeedDTO> = {}): InboxFeedDTO => ({
  items, quiet: items.length === 0, needs_you_count: items.length, policy_version: "v1",
  generated_at: "2026-09-11T08:00:00Z", dropped: [], trade_plan: null,
  liveness: { last_checked: "2026-09-11T08:00:00Z", pending_decisions: items.length, open_approvals: 0, cash_within_band: true, no_overdue_tasks: true, next_review: null },
  ...extra,
});
beforeEach(() => { getInbox.mockReset(); });

describe("Home action preview", () => {
  it("preserves server order, expands the remaining list and links to exact inbox cards without execution controls", async () => {
    getInbox.mockResolvedValue(feed([item("trade:8"), item("trade:2"), item("3"), item("4"), item("5"), item("6")]));
    render(<HomeActions userId="ariel" />);
    await screen.findByText("1. Review trade:8");
    expect(screen.getByText("2. Review trade:2")).toBeInTheDocument();
    expect(screen.getByText("Show 1 more actions (6 total)")).toBeInTheDocument();
    const target = screen.getAllByText("Review action →")[0].getAttribute("href")!;
    expect(decodeURIComponent(target.split("#")[1])).toBe(inboxItemAnchor("trade:8"));
    expect(screen.queryByRole("button", { name: "Execute now" })).not.toBeInTheDocument();
    expect(getInbox.mock.calls[0][1]).toBe(false);
    expect(getInbox.mock.calls[0][2]).toBeInstanceOf(AbortSignal);
  });

  it("labels expired records, missing deadlines and rationale instead of inventing them", async () => {
    getInbox.mockResolvedValue(feed([item("old", { expires_at: "2000-01-01T08:00:00", due_at: null, amount_usd: null, why_now: "" })]));
    render(<HomeActions userId="ariel" />);
    await screen.findByText(/Expired — needs reassessment/);
    expect(screen.getByText("Review expired proposal →")).toBeInTheDocument();
    expect(screen.getByText("No deadline recorded")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Why this action?"));
    expect(screen.getByText("No rationale supplied by this source.")).toBeInTheDocument();
  });

  it("does not equate an empty or partial list with a healthy system", async () => {
    getInbox.mockResolvedValue(feed([], { dropped: [{ reason: "source_error", id: "private-diagnostic" }] }));
    render(<HomeActions userId="ariel" />);
    await screen.findByText(/Some action sources failed/);
    expect(screen.getByText(/Check the analysis status above for coverage/)).toBeInTheDocument();
    expect(screen.queryByText(/private-diagnostic/)).not.toBeInTheDocument();
    expect(screen.getByText(/This is not the age of its underlying evidence/)).toBeInTheDocument();
  });

  it("removes prior actions on failed focus refresh and recovers on retry", async () => {
    getInbox.mockResolvedValueOnce(feed([item("old")])).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(feed([item("new")]));
    render(<HomeActions userId="ariel" />);
    await screen.findByText("1. Review old");
    await act(async () => window.dispatchEvent(new Event("focus")));
    await screen.findByRole("alert");
    expect(screen.queryByText("1. Review old")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry now" }));
    await screen.findByText("1. Review new");
  });

  it("keeps observations separate from the next-action list", async () => {
    getInbox.mockResolvedValue(feed([item("watch", { bucket: 6 })]));
    render(<HomeActions userId="ariel" />);
    await screen.findByText(/On the radar · 1 observations/);
    expect(screen.queryByLabelText("Prioritized next actions")).not.toBeInTheDocument();
  });

  it("aborts the request when unmounted", async () => {
    getInbox.mockReturnValue(new Promise(() => {}));
    const { unmount } = render(<HomeActions userId="ariel" />);
    await waitFor(() => expect(getInbox).toHaveBeenCalledOnce());
    const signal = getInbox.mock.calls[0][2] as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
  });
});

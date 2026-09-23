import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { DecisionReadinessBanner } from "../decision-readiness-banner";
import { KnowledgeFollowups } from "../home/KnowledgeFollowups";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it("keeps an explicitly urgent finding red and out of routine paperwork", async () => {
  const finding = {owner: "user", urgency: "urgent", claim: "Action due now",
    next_action: "Resolve the deadline", affected_advice: "Material imminent consequence"};
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ok: true, json: async () => ({
    status: "blocked", message: "A critical finding needs attention now", urgent_findings: [finding],
    knowledge: {status: "incomplete", documents: [{path: "rule.md", state: "incomplete", input_matches: true, findings: [finding]}]},
  })}));
  await act(async () => {render(<><DecisionReadinessBanner /><KnowledgeFollowups userId="ariel" /></>);});
  expect(screen.getByRole("alert")).toHaveTextContent("Material imminent consequence");
  expect(screen.getByText(/0 routine follow-ups/)).toBeInTheDocument();
});

it("does not re-request paperwork from a superseded input review", async () => {
  const fetcher = vi.fn().mockResolvedValue({ok: true, json: async () => ({
    status: "ready", message: "Current", knowledge: {status: "incomplete", documents: [{
      path: "old.md", state: "needs_review", input_matches: false,
      findings: [{owner: "user", next_action: "Already supplied statement"}],
    }]},
  })});
  vi.stubGlobal("fetch", fetcher);
  await act(async () => {render(<KnowledgeFollowups userId="someone else" />);});
  expect(screen.getByText(/0 routine follow-ups/)).toBeInTheDocument();
  expect(fetcher.mock.calls[0][0]).toBe("/api/health/decisions?user_id=someone%20else");
});

it("makes clear that pending reviews belong to Argosy, not the user", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    status: "degraded", message: "Review in progress", jobs: [{name: "knowledge_recheck", status: "running"}],
    knowledge: {status: "incomplete", total: 1, verified: 0, reported_verified: 0, documents: [
      {path: "tax/rule.md", state: "needs_review", findings: [], note: "Older review"},
    ]},
  }) }));
  await act(async () => { render(<KnowledgeFollowups userId="ariel" />); });
  expect(screen.getByText("tax/rule.md — Argosy review pending")).toBeInTheDocument();
  expect(screen.getByText(/Argosy is reviewing queued documents now/)).toBeInTheDocument();
  expect(screen.getByText(/No review action is needed from you/)).toBeInTheDocument();
});

it("separates incomplete knowledge from a crash and exposes owner, impact and next action", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    status: "ready", message: "Daily analysis jobs are current", jobs: [
      { name: "annual", status: "knowledge_incomplete", attention_required: true },
    ], knowledge: { status: "incomplete", total: 2, verified: 1, reported_verified: 1, paperwork_due_date: "2026-12-31", documents: [{
      path: "household/members.md", state: "incomplete", input_matches: true, note: "Dated facts retained", findings: [{
        scope: "current_balance", claim: "Account balance", missing_evidence: "Current statement",
        owner: "user", next_action: "Provide the latest statement", affected_advice: "Current allocation sizing", retry_after: null,
      }],
    }] },
  }) }));
  await act(async () => { render(<><DecisionReadinessBanner /><KnowledgeFollowups userId="ariel" /></>); });
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).not.toHaveTextContent("household/members.md");
  expect(screen.getByRole("status")).not.toHaveTextContent("jobs need attention");
  expect(screen.getByText("Due papers")).toBeInTheDocument();
  expect(screen.getByText(/Planned for 2026-12-31/)).toBeInTheDocument();
  expect(screen.getByText(/1 routine follow-ups/)).toBeInTheDocument();
  expect(screen.getByText(/Next action — You: Provide/)).toBeInTheDocument();
  expect(screen.getByText(/Affects: Current allocation sizing/)).toBeInTheDocument();
  expect(screen.getByText(/not a blocker for portfolio monitoring/)).toBeInTheDocument();
});

it("shows incomplete analysis and clears only after a fresh readiness poll", async () => {
  vi.useFakeTimers();
  const fetcher = vi.fn().mockResolvedValueOnce({
    ok: true, json: async () => ({ status: "blocked", message: "Runtime unavailable", pending_news: 77 }),
  }).mockResolvedValue({ ok: true, json: async () => ({ status: "ready", message: "Daily analysis jobs are current." }) });
  vi.stubGlobal("fetch", fetcher);
  await act(async () => { render(<DecisionReadinessBanner />); });
  expect(screen.getByRole("alert")).toHaveTextContent("Action required");
  expect(screen.getByRole("alert")).toHaveTextContent("77 news items");
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(screen.getByRole("status")).toHaveTextContent("Analysis up to date");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(2);
});

it("does not imply healthy analysis when the endpoint fails", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  await act(async () => { render(<DecisionReadinessBanner />); });
  expect(screen.getByRole("alert")).toHaveTextContent("Status unavailable");
});

it("shows the failed job, last success and sign-in action on the page", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    status: "blocked", message: "Authentication needs attention", jobs: [{
      name: "news_daily", status: "error", last_success_at: null,
      failure: { reason: "Claude could not load required managed settings.", action: "Sign in to Claude again, then retry." },
    }],
  }) }));
  await act(async () => { render(<DecisionReadinessBanner />); });
  expect(screen.getByRole("alert")).toHaveTextContent("news daily: error");
  expect(screen.getByRole("alert")).toHaveTextContent("Sign in to Claude again");
  expect(screen.getByRole("alert")).toHaveTextContent("No successful run recorded");
  expect(screen.getByRole("link")).toHaveAttribute("href", "/jobs");
});

it("does not silently stay healthy when a subsequent status request hangs", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce({ ok: true, json: async () => ({ status: "ready", message: "Current" }) })
    .mockImplementation((_url, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new Error("aborted")));
    })));
  await act(async () => { render(<DecisionReadinessBanner />); });
  await act(async () => { await vi.advanceTimersByTimeAsync(70_000); });
  expect(screen.getByRole("alert")).toHaveTextContent("Status unavailable");
});

it("treats an invalid response as unknown instead of hiding the status", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "ready" }) }));
  await act(async () => { render(<DecisionReadinessBanner />); });
  expect(screen.getByRole("alert")).toHaveTextContent("Status unavailable");
});

it("keeps disabled integration history visible without counting it as an active failure", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    status: "degraded", message: "Email needs configuration", jobs: [
      { name: "discord_listener", status: "disabled", attention_required: false },
      { name: "weekly_email_digest", status: "error", attention_required: true,
        failure: { reason: "Email delivery is not configured", action: "Configure SMTP" } },
    ],
  }) }));
  await act(async () => { render(<DecisionReadinessBanner />); });
  expect(screen.getByText(/1 jobs need attention/)).toBeInTheDocument();
  expect(screen.getByText(/Disabled by configuration: discord listener/)).toBeInTheDocument();
  expect(screen.getByText(/Email delivery is not configured/)).toBeInTheDocument();
});

"use client";

import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Paperclip, X } from "lucide-react";

import { AgentCascadePanel } from "@/components/advisor/AgentCascadePanel";
import { Markdown } from "@/components/markdown";
import { PlanInScopeCard } from "@/components/plan-in-scope-card";
import { PlanRevisionSheet } from "@/components/plan-revision-sheet";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  api,
  type AdvisorGapItem,
  type AdvisorGapsResponse,
  type AdvisorTurnResponse,
  type AmendmentEventPayload,
  type DraftResponse,
  type GapState,
} from "@/lib/api";
import {
  ensureNotificationPermission,
  notify,
  permission,
} from "@/lib/notifications";
import { useWSEvents } from "@/lib/ws";

// File extensions accepted by the answer-form attachment picker. Mirrors
// the server-side allowlist in argosy/services/turn_attachments.py.
// Text/markdown is appended inline to the user message; images and PDFs
// are forwarded as native Anthropic content blocks (images via vision,
// PDFs via the `document` block — full layout / table / OCR fidelity).
const ATTACH_ACCEPT = ".md,.markdown,.txt,.csv,.pdf,image/*,application/pdf";

const USER_ID = "ariel";

interface Turn {
  // The agent's message (question or answer to the user). Rendered as Markdown.
  agent_message: string;
  // The user's input (typed answer or free-form question).
  user_message: string;
  stage: string;
  confidence: string;
  mode: string;
}

const STAGE_NAMES: Record<string, string> = {
  stage_1: "Identity & Jurisdiction",
  stage_2: "Goals & Timeline",
  stage_3: "Financial Picture",
  stage_4: "Brokerage Connections",
  stage_5: "Plan Import & Critique",
  stage_6: "Operational Preferences",
  stage_7: "Estate Planning",
  stage_8: "Risk Management & Insurance",
  stage_9: "Tax Situation",
  stage_10: "Education Funding",
  complete: "Ongoing",
};

// Section ordering for the sidebar groupings.
const SECTION_ORDER: Array<"identity" | "goals" | "constraints"> = [
  "identity",
  "goals",
  "constraints",
];
const SECTION_LABELS: Record<string, string> = {
  identity: "Identity",
  goals: "Goals",
  constraints: "Constraints",
};

function dotColor(state: GapState): string {
  // Tailwind classes for the colored status dot.
  switch (state) {
    case "fresh":
      return "bg-success";
    case "stale":
      return "bg-warning";
    case "missing":
      return "bg-error";
  }
}

function dotLabel(state: GapState): string {
  switch (state) {
    case "fresh":
      return "Fresh";
    case "stale":
      return "Stale — please refresh";
    case "missing":
      return "Not yet answered";
  }
}

function formatTimestamp(iso: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    return d.toLocaleDateString();
  } catch {
    return "";
  }
}

export default function AdvisorPage() {
  // Conversation + agent-turn state.
  const [history, setHistory] = useState<Turn[]>([]);
  const [pending, setPending] = useState<AdvisorTurnResponse | null>(null);
  const [userInput, setUserInput] = useState("");
  const [loading, setLoading] = useState(true);

  // T4.7 — pre-seed the textarea from a ?seed= query param so the /plan
  // page's "Discuss with advisor" button can push a specific Fund Manager
  // objection straight into the advisor's input. Only fires once on mount;
  // subsequent edits to the textarea are the user's.
  const searchParams = useSearchParams();
  const seedConsumedRef = useRef(false);
  useEffect(() => {
    if (seedConsumedRef.current) return;
    const seed = searchParams.get("seed");
    if (seed && seed.trim()) {
      setUserInput(seed);
      seedConsumedRef.current = true;
    }
  }, [searchParams]);
  const [error, setError] = useState<string | null>(null);

  // Sidebar gap-tracker state.
  const [gaps, setGaps] = useState<AdvisorGapsResponse | null>(null);

  // In-flight guard: prevents concurrent askNext calls from a double-click
  // or rapid re-submit before the previous turn's loading flag has rerendered.
  const inFlightRef = useRef(false);

  // Answer/question attachment.
  const attachInputRef = useRef<HTMLInputElement | null>(null);
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // turnId — generated fresh for each advisor turn, passed to api.advisorTurn
  // so the backend echoes it in WS events. AgentCascadePanel filters on this.
  // Kept non-null after resolution so the panel stays visible; reset at the
  // TOP of the next askNext call (not in finally).
  const [currentTurnId, setCurrentTurnId] = useState<string | null>(null);

  // Real liveness signals during a long advisor turn. A bare spinner can
  // spin forever while the backend is dead; we instead track two
  // independent positive signals AND surface a red banner if either
  // breaks.
  //
  //   1. Periodic `/api/health` ping while `loading=true` — proves the
  //      HTTP path is alive end-to-end (uvicorn + DB).
  //   2. WS subscription to `agent.run.finished` events filtered by our
  //      USER_ID — proves the agent fleet is making forward progress,
  //      not just that the network is up.
  //
  // The visible "Thinking" panel reflects the latest state of both,
  // including elapsed-seconds counters for each. If health pings start
  // failing we override the spinner with a red banner so a dead backend
  // is impossible to miss.
  const [thinkingStartedAt, setThinkingStartedAt] = useState<number | null>(
    null,
  );
  const [thinkingNow, setThinkingNow] = useState<number>(() => Date.now());
  const [backendStatus, setBackendStatus] = useState<
    "unknown" | "ok" | "unreachable"
  >("unknown");
  const [lastHealthAt, setLastHealthAt] = useState<number | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [lastAgentStepAt, setLastAgentStepAt] = useState<number | null>(null);
  const [lastAgentStepRole, setLastAgentStepRole] = useState<string | null>(
    null,
  );

  // Tick every 250ms while a turn is in flight so the elapsed counters
  // visibly advance.
  useEffect(() => {
    if (thinkingStartedAt === null) return;
    const id = window.setInterval(() => setThinkingNow(Date.now()), 250);
    return () => window.clearInterval(id);
  }, [thinkingStartedAt]);

  // Periodic /api/health ping while a turn is in flight.
  useEffect(() => {
    if (thinkingStartedAt === null) return;
    let cancelled = false;
    const ping = async () => {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (cancelled) return;
        if (res.ok) {
          setBackendStatus("ok");
          setLastHealthAt(Date.now());
          setHealthError(null);
        } else {
          setBackendStatus("unreachable");
          setHealthError(`HTTP ${res.status}`);
        }
      } catch (e: unknown) {
        if (cancelled) return;
        setBackendStatus("unreachable");
        setHealthError(e instanceof Error ? e.message : String(e));
      }
    };
    void ping();
    const id = window.setInterval(ping, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [thinkingStartedAt]);

  // WS subscription — bumps lastAgentStepAt every time the backend
  // finishes an agent invocation for this user. This is the "work is
  // actually progressing" signal, distinct from "the network is up".
  const lastAgentRunEvent = useWSEvents<{
    user_id?: string;
    agent_role?: string;
  }>(["agent.run.finished"]);
  useEffect(() => {
    if (!lastAgentRunEvent) return;
    const { payload } = lastAgentRunEvent;
    if (payload.user_id !== USER_ID) return;
    setLastAgentStepAt(Date.now());
    setLastAgentStepRole(payload.agent_role ?? null);
  }, [lastAgentRunEvent]);

  const sinceHealthS =
    lastHealthAt !== null
      ? Math.floor((thinkingNow - lastHealthAt) / 1000)
      : null;
  const sinceAgentStepS =
    lastAgentStepAt !== null
      ? Math.floor((thinkingNow - lastAgentStepAt) / 1000)
      : null;

  // Wave 2: monthly plan-revision draft (banner + side sheet).
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [sheetOpen, setSheetOpen] = useState(false);

  // Wave 4: plan amendment chat flow.
  // - `activeAmendment` drives the in-flight status pill (Medium/Large only;
  //   Small applies inline and never has a "running" phase).
  // - `amendmentSystemMessage` is the in-app banner shown on completion or
  //   failure — the always-on fallback when the user did not grant browser
  //   notification permission.
  const [activeAmendment, setActiveAmendment] = useState<{
    decision_run_id: number;
    tier: "medium" | "large";
    eta_seconds: number;
    started_at: number;
  } | null>(null);
  const [amendmentSystemMessage, setAmendmentSystemMessage] = useState<
    string | null
  >(null);

  const refreshDraft = useCallback(async () => {
    try {
      const d = await api.planDraft(USER_ID);
      setDraft(d);
    } catch {
      // No pending draft (404) or transient error — banner stays hidden.
      setDraft(null);
    }
  }, []);

  useEffect(() => {
    refreshDraft();
  }, [refreshDraft]);

  // Wave 4: subscribe to plan.amendment.* events via the same /ws hook the
  // home + proposals pages use. `useWSEvents` returns the most recent
  // matching event; we react to it via the effect below.
  const lastAmendmentEvent = useWSEvents<AmendmentEventPayload>([
    "plan.amendment.started",
    "plan.amendment.completed",
    "plan.amendment.failed",
    "plan.amendment.cancelled",
  ]);

  useEffect(() => {
    if (!lastAmendmentEvent) return;
    const { event, payload } = lastAmendmentEvent;
    // Filter to this user — the /ws stream is shared across users.
    if (payload.user_id !== USER_ID) return;
    if (event === "plan.amendment.started") {
      if (payload.tier === "medium" || payload.tier === "large") {
        setActiveAmendment({
          decision_run_id: payload.decision_run_id,
          tier: payload.tier,
          eta_seconds:
            payload.eta_seconds ?? (payload.tier === "medium" ? 30 : 900),
          started_at: Date.now(),
        });
      }
    } else if (event === "plan.amendment.completed") {
      setActiveAmendment(null);
      setAmendmentSystemMessage("Plan revision ready — review it now.");
      refreshDraft();
      notify("Argosy", "Your plan revision is ready — review it now");
    } else if (event === "plan.amendment.failed") {
      setActiveAmendment(null);
      setAmendmentSystemMessage(
        `Plan amendment failed${payload.error ? `: ${payload.error}` : ""}.`,
      );
    } else if (event === "plan.amendment.cancelled") {
      setActiveAmendment(null);
      setAmendmentSystemMessage("Plan amendment cancelled.");
    }
  }, [lastAmendmentEvent, refreshDraft]);

  // Wave 4: prompt for browser-notification permission the first time a
  // Medium/Large amendment goes in-flight. Default state means we have not
  // asked yet; granted/denied skip the prompt.
  useEffect(() => {
    if (activeAmendment && permission() === "default") {
      void ensureNotificationPermission();
    }
  }, [activeAmendment]);

  const refreshGaps = useCallback(async () => {
    try {
      const g = await api.advisorGaps(USER_ID);
      setGaps(g);
    } catch (e: unknown) {
      // Non-fatal — the chat still works without the sidebar.
      console.warn("advisor.gaps.failed", e);
    }
  }, []);

  const askNext = useCallback(
    async (
      lastUserMessage: string,
      opts?: {
        targetField?: string;
        currentStage?: string;
        attachments?: File[];
      },
    ) => {
      // Guard against concurrent calls (double-click / rapid re-submit).
      // The ref is synchronous and survives between renders, unlike the
      // `loading` state which only prevents a second call after a re-render.
      if (inFlightRef.current) {
        return;
      }
      inFlightRef.current = true;

      // Reset the previous turn's cascade panel and generate a fresh turnId
      // for this call. We reset here (not in finally) so the PREVIOUS turn's
      // cascade panel stays visible until the next turn starts.
      const turnId = crypto.randomUUID();
      setCurrentTurnId(turnId);

      try {
        setLoading(true);
        // Reset all liveness signals — anything carried over from a
        // prior turn would be misleading.
        setBackendStatus("unknown");
        setLastHealthAt(null);
        setHealthError(null);
        setLastAgentStepAt(null);
        setLastAgentStepRole(null);
        setThinkingStartedAt(Date.now());
        setThinkingNow(Date.now());
        const t = await api.advisorTurn(USER_ID, lastUserMessage, {
          ...opts,
          turnId,
        });
        setPending(t);
      } catch (e: unknown) {
        // Surface turn failures through `submitError` so the message
        // shows in BOTH the sticky top banner AND the inline panel
        // under the input textarea. The middle-of-page `error` state
        // is reserved for fatal load errors that aren't tied to a
        // submission (none today, but kept as a hook).
        const msg = e instanceof Error ? e.message : String(e);
        setSubmitError(msg);
      } finally {
        inFlightRef.current = false;
        setLoading(false);
        setThinkingStartedAt(null);
        // After every turn, re-pull the sidebar so newly-fresh fields
        // light up green and counts stay accurate.
        await refreshGaps();
        // Note: currentTurnId is intentionally NOT reset here — we keep it
        // set so AgentCascadePanel continues to display the completed cascade.
        // It will be reset at the top of the NEXT call to askNext.
      }
    },
    [refreshGaps],
  );

  useEffect(() => {
    // On mount: pull sidebar gaps + fire a gap-driven turn so the agent
    // greets and asks the highest-priority missing field.
    refreshGaps().then(() => askNext(""));
  }, [refreshGaps, askNext]);

  const submit = async () => {
    if (!pending && !userInput.trim() && attachedFiles.length === 0) return;
    setSubmitError(null);

    const attachments = attachedFiles.slice();
    const summary =
      attachments.length > 0
        ? `[attached ${attachments.map((f) => f.name).join(", ")}]${
            userInput ? "\n" + userInput : ""
          }`
        : userInput;

    setHistory((h) => [
      ...h,
      {
        agent_message: pending?.question_for_user ?? "",
        user_message: summary,
        stage: pending?.stage ?? "stage_1",
        confidence: pending?.confidence ?? "MEDIUM",
        mode: pending?.mode ?? "gap_driven",
      },
    ]);
    const userText = userInput;
    setUserInput("");
    setAttachedFiles([]);
    if (attachInputRef.current) attachInputRef.current.value = "";
    setPending(null);
    await askNext(userText, {
      attachments: attachments.length > 0 ? attachments : undefined,
    });
  };

  // Sidebar row click → ask the agent to address THIS specific field
  // (target_field hint passed through to /api/advisor/turn).
  const handleGapClick = async (item: AdvisorGapItem) => {
    if (loading) return;
    setPending(null);
    await askNext("", { targetField: item.path });
  };

  const handleAttachChosen = (files: FileList | File[] | null) => {
    setSubmitError(null);
    if (!files) return;
    const newOnes = Array.from(files);
    if (newOnes.length === 0) return;
    setAttachedFiles((prev) => [...prev, ...newOnes]);
  };
  const removeAttachment = (idx: number) => {
    setAttachedFiles((prev) => prev.filter((_, i) => i !== idx));
    if (attachInputRef.current) attachInputRef.current.value = "";
  };
  // Drag-drop on the chat input. Wave 5 review I4: bind handlers to BOTH
  // the wrapper <div> and the inner <textarea>. The wrapper alone wasn't
  // enough — some browsers still let the textarea handle the drop natively
  // and insert the OS file path as text. `stopPropagation` is required so
  // the textarea's drop event doesn't bubble back to the wrapper and
  // double-attach (post-review fix).
  const handleChatDrop = (
    e: React.DragEvent<HTMLDivElement | HTMLTextAreaElement>,
  ) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleAttachChosen(e.dataTransfer.files);
    }
  };
  const handleChatDragOver = (
    e: React.DragEvent<HTMLDivElement | HTMLTextAreaElement>,
  ) => {
    e.preventDefault();
    e.stopPropagation();
  };
  // Paste a screenshot into the textarea. Wave 5 review M4: if the user
  // pastes a non-image file (PDF, docx, ...), surface a hint instead of
  // silently swallowing it — the previous handler dropped non-image files
  // with no feedback at all.
  const handleChatPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const items = e.clipboardData?.items ?? [];
    const imageFiles: File[] = [];
    let nonImageFileSeen = false;
    for (const item of items) {
      if (item.kind === "file") {
        if (item.type.startsWith("image/")) {
          const f = item.getAsFile();
          if (f) {
            // Synthesize a friendly filename if the clipboard didn't supply one
            const renamed = f.name && f.name !== ""
              ? f
              : new File([f], `pasted-${Date.now()}.png`, { type: f.type });
            imageFiles.push(renamed);
          }
        } else {
          nonImageFileSeen = true;
        }
      }
    }
    if (imageFiles.length > 0) {
      e.preventDefault();
      handleAttachChosen(imageFiles);
    } else if (nonImageFileSeen) {
      e.preventDefault();
      setSubmitError(
        "Pasted file isn't an image. For text/markdown documents, click " +
        "the paperclip below or drag the file onto the chat box.",
      );
    }
  };

  // ---- Sidebar grouping ---------------------------------------------
  const groupedGaps = SECTION_ORDER.map((sec) => ({
    section: sec,
    items: (gaps?.items ?? []).filter((it) => it.section === sec),
  }));

  return (
    <main className="max-w-7xl mx-auto p-6 flex flex-col gap-6">
      {/* Sticky error banner — duplicates the bottom-of-form error so a
          rejection (e.g. unsupported attachment type) stays visible no
          matter how far the user has scrolled. Click to dismiss. */}
      {submitError && (
        <div
          role="alert"
          onClick={() => setSubmitError(null)}
          className="sticky top-2 z-30 rounded-md border border-error/40 bg-error/15 backdrop-blur p-3 cursor-pointer hover:bg-error/25 transition"
        >
          <p className="text-sm text-error font-mono flex items-center justify-between gap-3">
            <span>{submitError}</span>
            <span className="text-xs text-error/70 shrink-0">
              click to dismiss
            </span>
          </p>
        </div>
      )}
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Advisor</h1>
        <p className="text-sm text-muted-foreground">
          Persistent advisor panel: ask anything, share new info, and watch
          the gap tracker turn green as we close out missing context.
          {gaps?.current_stage && (
            <>
              {" "}
              Current focus:{" "}
              <span className="text-foreground font-mono">
                {STAGE_NAMES[gaps.current_stage] ?? gaps.current_stage}
              </span>
              .
            </>
          )}
        </p>
      </header>

      {error && <p className="text-sm text-error font-mono">{error}</p>}

      {activeAmendment && (
        <div className="rounded-md border border-warning/40 bg-warning/10 p-3 flex items-center justify-between text-sm">
          <span>
            Plan amendment in progress (
            <strong>{activeAmendment.tier}</strong> · ETA{" "}
            {activeAmendment.tier === "medium" ? "~30s" : "~15 min"})
          </span>
          <Button
            size="sm"
            variant="outline"
            onClick={async () => {
              try {
                await api.advisorAmendmentCancel(
                  USER_ID,
                  activeAmendment.decision_run_id,
                );
              } catch {
                // Server may have completed in the meantime; refresh the
                // surface so the pill clears regardless.
                setActiveAmendment(null);
              }
            }}
          >
            Cancel
          </Button>
        </div>
      )}

      {amendmentSystemMessage && (
        <div className="rounded-md border border-success/30 bg-success/10 p-3 text-sm">
          {amendmentSystemMessage}
          <button
            type="button"
            className="ml-2 text-xs text-muted-foreground hover:underline"
            onClick={() => setAmendmentSystemMessage(null)}
          >
            dismiss
          </button>
        </div>
      )}

      {draft && (
        <div className="rounded-md border border-error/40 bg-error/10 p-3 flex items-center justify-between">
          <p className="text-sm">
            Draft plan ready (synthesized{" "}
            {new Date(draft.drafted_at).toLocaleDateString()}) —{" "}
            <strong>
              {(draft.horizon_long?.deltas_from_prior.length ?? 0) +
                (draft.horizon_medium?.deltas_from_prior.length ?? 0) +
                (draft.horizon_short?.deltas_from_prior.length ?? 0)}
            </strong>{" "}
            delta(s) vs. last month.
          </p>
          <Button size="sm" onClick={() => setSheetOpen(true)}>
            Review now
          </Button>
        </div>
      )}

      {draft && (
        <PlanRevisionSheet
          open={sheetOpen}
          onOpenChange={setSheetOpen}
          userId={USER_ID}
          draft={draft}
          onAccepted={() => {
            setSheetOpen(false);
            refreshDraft();
          }}
          onRejected={() => {
            setSheetOpen(false);
            refreshDraft();
          }}
        />
      )}

      <PlanInScopeCard userId={USER_ID} />

      <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_320px] gap-6">
        {/* ---- Main column: chat (uploads now happen inline below) ---- */}
        <div className="flex flex-col gap-6 min-w-0">
          {/* Chat surface. */}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Conversation</CardTitle>
              <CardDescription>
                Type a question, share an update, or click a row in the tracker
                on the right to fill a specific gap.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {history.map((turn, idx) => (
                <div key={idx} className="flex flex-col gap-1">
                  {turn.agent_message && (
                    <div className="text-sm">
                      <span className="font-semibold">Advisor:</span>{" "}
                      <div className="inline-block align-top w-full">
                        <Markdown>{turn.agent_message}</Markdown>
                      </div>
                    </div>
                  )}
                  {turn.user_message && (
                    <p className="text-sm text-muted-foreground whitespace-pre-wrap">
                      <span className="font-semibold text-foreground">You:</span>{" "}
                      {turn.user_message}
                    </p>
                  )}
                </div>
              ))}

              {/* Backend-unreachable banner — shown only when health pings
                  fail while a turn is in-flight. Separate from the cascade
                  panel so it stays prominent even when cascade has no rows. */}
              {loading && backendStatus === "unreachable" && (
                <div
                  className="rounded-md border border-error/40 bg-error/10 text-error p-3 text-xs font-mono"
                  aria-live="polite"
                >
                  <div className="flex flex-col gap-1">
                    <p className="text-sm">
                      ⚠ Backend unreachable
                      {sinceHealthS !== null && (
                        <span className="ml-2 text-error/80">
                          (last contact {sinceHealthS}s ago)
                        </span>
                      )}
                    </p>
                    {healthError && (
                      <p className="text-[10px] text-error/70">
                        {healthError}
                      </p>
                    )}
                    <p className="text-[10px] text-error/70">
                      The advisor turn may still complete if the backend
                      comes back; otherwise refresh the page.
                    </p>
                  </div>
                </div>
              )}

              {/* Live cascade panel — replaces the old "Thinking..." spinner.
                  Visible while a turn is in-flight AND after it resolves
                  (until the next turn starts). The diagnosticLine prop
                  carries the backend-status / last-agent-step telemetry,
                  visually subordinated below the card stack. */}
              {currentTurnId !== null && (
                <AgentCascadePanel
                  userId={USER_ID}
                  turnId={currentTurnId}
                  isResolved={!loading}
                  diagnosticLine={
                    <span>
                      backend{" "}
                      {backendStatus === "ok" ? (
                        <span className="text-success">
                          OK
                          {sinceHealthS !== null && (
                            <> ({sinceHealthS}s ago)</>
                          )}
                        </span>
                      ) : backendStatus === "unreachable" ? (
                        <span className="text-error">unreachable</span>
                      ) : (
                        <span>checking…</span>
                      )}{" "}
                      ·{" "}
                      {lastAgentStepAt !== null ? (
                        <span className="text-success">
                          last agent step{" "}
                          {lastAgentStepRole && (
                            <span className="text-muted-foreground">
                              ({lastAgentStepRole})
                            </span>
                          )}{" "}
                          {sinceAgentStepS}s ago
                        </span>
                      ) : (
                        <span>no agent steps yet</span>
                      )}
                    </span>
                  }
                />
              )}

              {pending && pending.question_for_user && (
                <div className="text-sm font-semibold">
                  <Markdown>{pending.question_for_user}</Markdown>
                </div>
              )}
              {pending && pending.notes_for_orchestrator && (
                <p className="text-xs text-warning">
                  Note: {pending.notes_for_orchestrator}
                </p>
              )}

              {/* User input — text + drag-drop + paste-image + file picker.
                  Wave 5: a single composite surface replaces the separate
                  "Have an existing plan?" widget. Drop a markdown file
                  (it'll be ingested as a baseline plan), paste a screenshot
                  (forwarded as an image to vision-capable backends), or
                  click the paperclip. */}
              <div
                className="flex flex-col gap-2"
                onDragOver={handleChatDragOver}
                onDrop={handleChatDrop}
              >
                <textarea
                  className="bg-background border border-border rounded-md px-3 py-2 text-sm font-mono min-h-[80px]"
                  value={userInput}
                  onChange={(e) => setUserInput(e.target.value)}
                  onPaste={handleChatPaste}
                  onDragOver={handleChatDragOver}
                  onDrop={handleChatDrop}
                  placeholder="Type a question, drop a file, or paste a screenshot..."
                />

                {/* Attached-file pills. */}
                {attachedFiles.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {attachedFiles.map((f, idx) => (
                      <span
                        key={`${f.name}-${idx}`}
                        className="inline-flex items-center gap-1 text-xs bg-secondary/60 border border-border rounded-md px-2 py-1 font-mono"
                      >
                        <Paperclip className="h-3 w-3" aria-hidden suppressHydrationWarning />
                        {f.name}
                        <button
                          type="button"
                          onClick={() => removeAttachment(idx)}
                          aria-label={`remove ${f.name}`}
                          className="text-muted-foreground hover:text-foreground"
                        >
                          <X className="h-3 w-3" aria-hidden suppressHydrationWarning />
                        </button>
                      </span>
                    ))}
                  </div>
                )}

                {/* Attach button row. */}
                <div className="flex flex-wrap items-center gap-3 text-xs">
                  <label
                    htmlFor="advisor-attach-input"
                    className="cursor-pointer inline-flex items-center gap-1 text-primary underline-offset-4 hover:underline"
                  >
                    <Paperclip className="h-3 w-3" aria-hidden suppressHydrationWarning />
                    Attach
                  </label>
                  <input
                    id="advisor-attach-input"
                    ref={attachInputRef}
                    type="file"
                    accept={ATTACH_ACCEPT}
                    multiple
                    className="hidden"
                    onChange={(e) => handleAttachChosen(e.target.files)}
                  />
                  <span className="text-muted-foreground">
                    (text/markdown, images, or PDFs — 10 MB per file, 20 MB total)
                  </span>
                </div>

                {submitError && (
                  <div className="rounded-md border border-error/40 bg-error/10 p-3">
                    <p className="text-sm text-error font-mono">
                      {submitError}
                    </p>
                  </div>
                )}

                <div className="flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">
                    {pending && (
                      <>
                        Mode:{" "}
                        <span className="font-mono">{pending.mode}</span> ·
                        Confidence: {pending.confidence}
                      </>
                    )}
                  </span>
                  <Button
                    onClick={submit}
                    size="sm"
                    disabled={loading || (!userInput.trim() && attachedFiles.length === 0)}
                  >
                    Send
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>

        {/* ---- Sidebar: gap tracker ---- */}
        <aside className="md:sticky md:top-20 md:self-start">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Gap tracker</CardTitle>
              <CardDescription>
                {gaps ? (
                  <>
                    <span className="text-success">{gaps.counts.fresh}</span>{" "}
                    fresh ·{" "}
                    <span className="text-warning">{gaps.counts.stale}</span>{" "}
                    stale ·{" "}
                    <span className="text-error">{gaps.counts.missing}</span>{" "}
                    missing
                  </>
                ) : (
                  "Loading..."
                )}
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {groupedGaps.map(({ section, items }) =>
                items.length === 0 ? null : (
                  <div key={section} className="flex flex-col gap-1">
                    <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                      {SECTION_LABELS[section]}
                    </p>
                    <ul className="flex flex-col">
                      {items.map((item) => (
                        <li key={item.path}>
                          <button
                            type="button"
                            onClick={() => handleGapClick(item)}
                            disabled={loading}
                            title={`${dotLabel(item.state)} · ${item.freshness}`}
                            className="w-full text-left flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-accent/40 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                          >
                            <span
                              className={`inline-block h-2 w-2 rounded-full shrink-0 ${dotColor(item.state)}`}
                              aria-label={dotLabel(item.state)}
                            />
                            <span className="flex-1 text-xs">{item.label}</span>
                            {item.last_updated && (
                              <span className="text-[10px] font-mono text-muted-foreground">
                                {formatTimestamp(item.last_updated)}
                              </span>
                            )}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                ),
              )}
            </CardContent>
          </Card>
        </aside>
      </div>
    </main>
  );
}

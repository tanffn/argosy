"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Markdown } from "@/components/markdown";
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
  type GapState,
  type IntakeUploadResponse,
} from "@/lib/api";

// File extensions accepted by the answer-form attachment picker. Mirrors
// argosy/ingest/file_to_text.py's _EXT_TO_KIND whitelist.
const ATTACH_ACCEPT = ".md,.markdown,.txt,.csv,.tsv,.pdf,.xlsx";

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
      return "bg-emerald-500";
    case "stale":
      return "bg-amber-500";
    case "missing":
      return "bg-red-500";
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
  const [error, setError] = useState<string | null>(null);

  // Sidebar gap-tracker state.
  const [gaps, setGaps] = useState<AdvisorGapsResponse | null>(null);

  // Upload widget (re-used from the legacy intake page).
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<IntakeUploadResponse | null>(
    null,
  );
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadCollapsed, setUploadCollapsed] = useState(false);

  // Answer/question attachment.
  const attachInputRef = useRef<HTMLInputElement | null>(null);
  const [attachedFile, setAttachedFile] = useState<File | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);

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
      opts?: { targetField?: string; currentStage?: string },
    ) => {
      try {
        setLoading(true);
        const t = await api.advisorTurn(USER_ID, lastUserMessage, opts);
        setPending(t);
      } catch (e: unknown) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoading(false);
        // After every turn, re-pull the sidebar so newly-fresh fields
        // light up green and counts stay accurate.
        await refreshGaps();
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
    if (!pending && !userInput.trim() && !attachedFile) return;
    setSubmitError(null);

    let augmented = userInput;
    if (attachedFile) {
      try {
        const r = await api.intakeFileToText(attachedFile);
        const fenced =
          "I've attached " +
          r.filename +
          ":\n```\n" +
          r.extracted_text +
          "\n```\n\n";
        augmented = fenced + (userInput || "");
      } catch (e: unknown) {
        setSubmitError(e instanceof Error ? e.message : String(e));
        return;
      }
    }

    setHistory((h) => [
      ...h,
      {
        agent_message: pending?.question_for_user ?? "",
        user_message: attachedFile
          ? `[attached ${attachedFile.name}]${userInput ? "\n" + userInput : ""}`
          : userInput,
        stage: pending?.stage ?? "stage_1",
        confidence: pending?.confidence ?? "MEDIUM",
        mode: pending?.mode ?? "gap_driven",
      },
    ]);
    setUserInput("");
    setAttachedFile(null);
    if (attachInputRef.current) attachInputRef.current.value = "";
    setPending(null);
    await askNext(augmented);
  };

  // Sidebar row click → ask the agent to address THIS specific field
  // (target_field hint passed through to /api/advisor/turn).
  const handleGapClick = async (item: AdvisorGapItem) => {
    if (loading) return;
    setPending(null);
    await askNext("", { targetField: item.path });
  };

  const handleAttachChosen = (f: File | null) => {
    setSubmitError(null);
    setAttachedFile(f);
  };
  const removeAttachment = () => {
    setAttachedFile(null);
    if (attachInputRef.current) attachInputRef.current.value = "";
  };

  // ---- Upload handlers (carried over from legacy intake page) -------
  const handleFileChosen = (f: File | null) => {
    setUploadError(null);
    if (f && !f.name.toLowerCase().endsWith(".md")) {
      setUploadError("Please choose a Markdown (.md) file.");
      setSelectedFile(null);
      return;
    }
    setSelectedFile(f);
  };
  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    const f = e.dataTransfer.files?.[0];
    if (f) handleFileChosen(f);
  };
  const onUpload = async () => {
    if (!selectedFile) return;
    setUploading(true);
    setUploadError(null);
    try {
      const result = await api.intakeUpload(USER_ID, selectedFile);
      setUploadResult(result);
      setSelectedFile(null);
    } catch (e: unknown) {
      setUploadError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };
  const onContinueAfterUpload = async () => {
    setUploadCollapsed(true);
    setPending(null);
    await askNext("");
  };
  const reopenUpload = () => {
    setUploadCollapsed(false);
    setUploadResult(null);
    setSelectedFile(null);
    setUploadError(null);
  };

  // ---- Sidebar grouping ---------------------------------------------
  const groupedGaps = SECTION_ORDER.map((sec) => ({
    section: sec,
    items: (gaps?.items ?? []).filter((it) => it.section === sec),
  }));

  return (
    <main className="max-w-7xl mx-auto p-6 flex flex-col gap-6">
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

      {error && <p className="text-sm text-red-500 font-mono">{error}</p>}

      <div className="grid grid-cols-1 md:grid-cols-[minmax(0,1fr)_320px] gap-6">
        {/* ---- Main column: chat + upload ---- */}
        <div className="flex flex-col gap-6 min-w-0">
          {/* Plan-upload widget. */}
          {uploadCollapsed ? (
            <p className="text-xs text-muted-foreground">
              Plan uploaded.{" "}
              <button
                type="button"
                onClick={reopenUpload}
                className="text-primary underline-offset-4 hover:underline"
              >
                Upload another plan
              </button>
            </p>
          ) : (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">
                  Have an existing plan?
                </CardTitle>
                <CardDescription>
                  Upload a Markdown plan and I&apos;ll only ask about
                  what&apos;s missing.
                </CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                {!uploadResult && (
                  <>
                    <div
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={handleDrop}
                      onClick={() => fileInputRef.current?.click()}
                      className="border border-dashed border-border rounded-md p-6 text-center cursor-pointer hover:bg-accent/30 transition-colors"
                    >
                      <p className="text-sm">
                        {selectedFile
                          ? selectedFile.name
                          : "Drop a .md file here, or click to choose"}
                      </p>
                      <input
                        ref={fileInputRef}
                        type="file"
                        accept=".md,text/markdown"
                        className="hidden"
                        onChange={(e) =>
                          handleFileChosen(e.target.files?.[0] ?? null)
                        }
                      />
                    </div>

                    {uploadError && (
                      <div className="rounded-md border border-red-500/40 bg-red-500/10 p-3">
                        <p className="text-sm text-red-500 font-mono">
                          {uploadError}
                        </p>
                      </div>
                    )}

                    <div className="flex items-center justify-end gap-2">
                      <Button
                        onClick={onUpload}
                        disabled={!selectedFile || uploading}
                        size="sm"
                      >
                        {uploading ? (
                          <span className="inline-flex items-center gap-2">
                            <span
                              className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent"
                              aria-hidden
                            />
                            Extracting...
                          </span>
                        ) : (
                          "Upload plan"
                        )}
                      </Button>
                    </div>
                  </>
                )}

                {uploadResult && (
                  <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-4 flex flex-col gap-3">
                    <p className="text-sm font-semibold text-emerald-600 dark:text-emerald-400">
                      {uploadResult.summary_for_user}
                    </p>
                    <div className="flex justify-end">
                      <Button onClick={onContinueAfterUpload} size="sm">
                        Continue
                      </Button>
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>
          )}

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

              {loading && (
                <p className="text-sm text-muted-foreground">Thinking...</p>
              )}

              {pending && pending.question_for_user && (
                <div className="text-sm font-semibold">
                  <Markdown>{pending.question_for_user}</Markdown>
                </div>
              )}
              {pending && pending.notes_for_orchestrator && (
                <p className="text-xs text-amber-500">
                  Note: {pending.notes_for_orchestrator}
                </p>
              )}

              {/* User input (always available — questions OR answers). */}
              <div className="flex flex-col gap-2">
                <textarea
                  className="bg-background border border-border rounded-md px-3 py-2 text-sm font-mono min-h-[80px]"
                  value={userInput}
                  onChange={(e) => setUserInput(e.target.value)}
                  placeholder="Type a question or share an update..."
                />

                {/* Attachment row. */}
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <label
                    htmlFor="advisor-attach-input"
                    className="cursor-pointer text-primary underline-offset-4 hover:underline"
                  >
                    {attachedFile ? "Replace attachment" : "Attach a file"}
                  </label>
                  <input
                    id="advisor-attach-input"
                    ref={attachInputRef}
                    type="file"
                    accept={ATTACH_ACCEPT}
                    className="hidden"
                    onChange={(e) =>
                      handleAttachChosen(e.target.files?.[0] ?? null)
                    }
                  />
                  {attachedFile && (
                    <>
                      <span className="text-muted-foreground font-mono">
                        {attachedFile.name}
                      </span>
                      <button
                        type="button"
                        onClick={removeAttachment}
                        className="text-red-500 underline-offset-4 hover:underline"
                      >
                        remove
                      </button>
                    </>
                  )}
                  <span className="text-muted-foreground">
                    (.md, .markdown, .txt, .csv, .tsv, .pdf, .xlsx — 5 MB max)
                  </span>
                </div>

                {submitError && (
                  <div className="rounded-md border border-red-500/40 bg-red-500/10 p-3">
                    <p className="text-sm text-red-500 font-mono">
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
                    disabled={loading || (!userInput.trim() && !attachedFile)}
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
                    <span className="text-emerald-500">{gaps.counts.fresh}</span>{" "}
                    fresh ·{" "}
                    <span className="text-amber-500">{gaps.counts.stale}</span>{" "}
                    stale ·{" "}
                    <span className="text-red-500">{gaps.counts.missing}</span>{" "}
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

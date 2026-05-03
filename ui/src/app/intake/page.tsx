"use client";

import { useCallback, useEffect, useRef, useState } from "react";

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
  type IntakeTurnResponse,
  type IntakeUploadResponse,
} from "@/lib/api";

const USER_ID = "ariel";

interface Turn {
  question: string;
  answer: string;
  stage: string;
  confidence: string;
}

const STAGE_NAMES: Record<string, string> = {
  stage_1: "Identity & Jurisdiction",
  stage_2: "Goals & Timeline",
  stage_3: "Financial Picture",
  stage_4: "Brokerage Connections",
  stage_5: "Plan Import & Critique",
  stage_6: "Operational Preferences",
  complete: "Complete",
};

export default function IntakePage() {
  const [stage, setStage] = useState<string>("stage_1");
  const [history, setHistory] = useState<Turn[]>([]);
  const [pending, setPending] = useState<IntakeTurnResponse | null>(null);
  const [answer, setAnswer] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // ---- Upload widget state ------------------------------------------
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<IntakeUploadResponse | null>(
    null,
  );
  const [uploadError, setUploadError] = useState<string | null>(null);
  // After a successful upload the widget collapses; user can re-expand it.
  const [uploadCollapsed, setUploadCollapsed] = useState(false);

  const loadStatus = useCallback(async () => {
    try {
      const s = await api.intakeStatus(USER_ID);
      setStage(s.current_stage);
    } catch (e: unknown) {
      setError(String(e));
    }
  }, []);

  const askNext = useCallback(
    async (lastAnswer: string) => {
      try {
        setLoading(true);
        const t = await api.intakeTurn(USER_ID, lastAnswer, stage);
        setPending(t);
        if (t.next_stage) {
          setStage(t.next_stage === "complete" ? "complete" : t.next_stage);
        }
      } catch (e: unknown) {
        setError(String(e));
      } finally {
        setLoading(false);
      }
    },
    [stage],
  );

  useEffect(() => {
    loadStatus().then(() => askNext(""));
  }, [loadStatus, askNext]);

  const submit = async () => {
    if (!pending) return;
    setHistory((h) => [
      ...h,
      {
        question: pending.question_for_user,
        answer,
        stage: pending.stage,
        confidence: pending.confidence,
      },
    ]);
    const a = answer;
    setAnswer("");
    setPending(null);
    await askNext(a);
  };

  // ---- Upload handlers ----------------------------------------------
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
    // Refresh status + restart the turn loop so the next question reflects
    // the freshly-merged user_context.
    await loadStatus();
    setPending(null);
    await askNext("");
  };

  const reopenUpload = () => {
    setUploadCollapsed(false);
    setUploadResult(null);
    setSelectedFile(null);
    setUploadError(null);
  };

  return (
    <main className="max-w-3xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Intake</h1>
        <p className="text-sm text-muted-foreground">
          Re-runnable 6-stage interview. The intake agent asks one question per
          turn; your answers update `user_context`.
        </p>
      </header>

      {error && <p className="text-sm text-red-500 font-mono">{error}</p>}

      {/* ---- Upload widget ---- */}
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
            <CardTitle className="text-base">Have an existing plan?</CardTitle>
            <CardDescription>
              Upload a Markdown plan and I&apos;ll only ask about what&apos;s
              missing.
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
                <div className="flex flex-col gap-1">
                  <p className="text-xs font-semibold">
                    Extracted from your plan:
                  </p>
                  {uploadResult.fields_extracted.length > 0 ? (
                    <ul className="text-xs text-muted-foreground list-disc pl-5">
                      {uploadResult.fields_extracted.map((f) => (
                        <li key={f}>{f}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      (none — the plan didn&apos;t map cleanly)
                    </p>
                  )}
                </div>
                <div className="flex flex-col gap-1">
                  <p className="text-xs font-semibold">
                    Still need to ask about:
                  </p>
                  {uploadResult.fields_missing.length > 0 ? (
                    <ul className="text-xs text-muted-foreground list-disc pl-5">
                      {uploadResult.fields_missing.map((f) => (
                        <li key={f}>{f}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-xs text-muted-foreground">
                      (nothing — the plan covered everything)
                    </p>
                  )}
                </div>
                <div className="flex flex-col gap-0.5">
                  <p className="text-xs">
                    <span className="font-semibold">Confidence:</span>{" "}
                    {uploadResult.confidence}
                  </p>
                  {uploadResult.notes && (
                    <p className="text-xs text-muted-foreground">
                      <span className="font-semibold text-foreground">
                        Notes:
                      </span>{" "}
                      {uploadResult.notes}
                    </p>
                  )}
                </div>
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

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Current stage: {STAGE_NAMES[stage] ?? stage}
          </CardTitle>
          <CardDescription>
            Confidence flags appear inline; missing-data warnings surface as
            agent notes.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {history.map((turn, idx) => (
            <div key={idx} className="flex flex-col gap-1">
              <p className="text-sm">
                <span className="font-semibold">Q:</span> {turn.question}
              </p>
              <p className="text-sm text-muted-foreground">
                <span className="font-semibold text-foreground">A:</span>{" "}
                {turn.answer}
              </p>
            </div>
          ))}

          {loading && <p className="text-sm text-muted-foreground">Thinking...</p>}

          {pending && pending.question_for_user && (
            <div className="flex flex-col gap-2">
              <p className="text-sm font-semibold">{pending.question_for_user}</p>
              {pending.notes_for_orchestrator && (
                <p className="text-xs text-amber-500">
                  Note: {pending.notes_for_orchestrator}
                </p>
              )}
              <textarea
                className="bg-background border border-border rounded-md px-3 py-2 text-sm font-mono min-h-[80px]"
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                placeholder="Your answer..."
              />
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted-foreground">
                  Confidence: {pending.confidence}
                </span>
                <Button onClick={submit} size="sm" disabled={!answer.trim()}>
                  Submit
                </Button>
              </div>
            </div>
          )}

          {pending && pending.stage_complete && (
            <p className="text-sm text-emerald-500">
              Stage complete — moving to {pending.next_stage ?? "(no next stage)"}
            </p>
          )}
        </CardContent>
      </Card>
    </main>
  );
}

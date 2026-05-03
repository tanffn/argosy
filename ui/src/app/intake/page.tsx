"use client";

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type IntakeTurnResponse } from "@/lib/api";

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

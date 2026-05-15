"use client";

import { useCallback, useEffect, useState } from "react";

import { Markdown } from "@/components/markdown";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type PlanCurrentDTO } from "@/lib/api";

const USER_ID = "ariel";

interface Finding {
  plan_item_ref: string;
  severity: "RED" | "YELLOW" | "GREEN";
  topic: string;
  summary: string;
  evidence: string[];
  cited_sources: string[];
  recommended_action: string | null;
}

interface CritiqueShape {
  plan_label?: string;
  overall_summary?: string;
  findings?: Finding[];
}

export default function PlanPage() {
  const [plan, setPlan] = useState<PlanCurrentDTO | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.planCurrent(USER_ID);
      setPlan(data);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const onRecritique = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      await api.recritique(USER_ID);
      await refresh();
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setRunning(false);
    }
  }, [refresh]);

  const critique = (plan?.latest_critique_json as CritiqueShape | null) ?? null;
  const findings = critique?.findings ?? [];

  return (
    <main className="max-w-5xl mx-auto p-6 flex flex-col gap-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Plan</h1>
          <p className="text-sm text-muted-foreground">
            {plan?.version_label
              ? `Latest: ${plan.version_label}`
              : "No plan imported yet."}
          </p>
        </div>
        <Button
          variant="default"
          onClick={onRecritique}
          disabled={running || !plan?.plan_version_id}
        >
          {running ? "Re-critiquing…" : "Re-critique now"}
        </Button>
      </header>

      {error && <p className="text-sm text-error font-mono">{error}</p>}
      {loading && <p className="text-sm text-muted-foreground">Loading…</p>}

      {critique && (
        <Card>
          <CardHeader>
            <CardTitle>Critique findings</CardTitle>
            <CardDescription>{critique.overall_summary}</CardDescription>
          </CardHeader>
          <CardContent>
            {findings.length === 0 ? (
              <p className="text-sm text-muted-foreground">No findings recorded.</p>
            ) : (
              <ul className="flex flex-col gap-3">
                {findings.map((f, i) => (
                  <li
                    key={`${f.plan_item_ref}-${i}`}
                    className="p-3 rounded-md border border-border/60 bg-muted/20"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-medium">{f.topic}</span>
                      <Badge variant={severityVariant(f.severity)}>
                        {f.severity}
                      </Badge>
                    </div>
                    <p className="text-xs text-muted-foreground mt-1">
                      {f.plan_item_ref}
                    </p>
                    <p className="text-sm mt-2">{f.summary}</p>
                    {f.evidence.length > 0 && (
                      <ul className="text-xs text-muted-foreground mt-2 list-disc list-inside">
                        {f.evidence.map((e, j) => (
                          <li key={j}>{e}</li>
                        ))}
                      </ul>
                    )}
                    {f.cited_sources.length > 0 && (
                      <p className="text-xs font-mono text-muted-foreground mt-1">
                        cite: {f.cited_sources.join(", ")}
                      </p>
                    )}
                    {f.recommended_action && (
                      <p className="text-xs mt-2">
                        <span className="font-semibold">Action:</span>{" "}
                        {f.recommended_action}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      {plan?.raw_markdown ? (
        <Card>
          <CardHeader>
            <CardTitle>Plan document</CardTitle>
          </CardHeader>
          <CardContent>
            <Markdown>{plan.raw_markdown}</Markdown>
          </CardContent>
        </Card>
      ) : (
        !loading && (
          <p className="text-sm text-muted-foreground">
            Run <code>argosy ingest plan &lt;path&gt;</code> to import a plan.
          </p>
        )
      )}
    </main>
  );
}

function severityVariant(
  severity: string,
): "default" | "secondary" | "destructive" | "success" | "error" | "outline" {
  switch (severity) {
    case "RED":
      return "error";
    case "YELLOW":
      return "secondary";
    case "GREEN":
      return "success";
    default:
      return "outline";
  }
}

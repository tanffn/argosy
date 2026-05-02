"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

type HealthState = "loading" | "ok" | "error";

interface HealthResponse {
  status: "ok" | "error";
  db: "ok" | "error";
  version: string;
}

export default function Home() {
  const [state, setState] = useState<HealthState>("loading");
  const [details, setDetails] = useState<HealthResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function fetchHealth() {
      try {
        const res = await fetch("/api/health", { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body: HealthResponse = await res.json();
        if (cancelled) return;
        setDetails(body);
        setState(body.status === "ok" ? "ok" : "error");
      } catch {
        if (cancelled) return;
        setState("error");
        setDetails(null);
      }
    }
    fetchHealth();
    return () => {
      cancelled = true;
    };
  }, []);

  let badge: React.ReactElement;
  if (state === "loading") {
    badge = <Badge variant="secondary">Loading...</Badge>;
  } else if (state === "ok") {
    badge = <Badge variant="success">Health: OK</Badge>;
  } else {
    badge = <Badge variant="error">Health: ERROR</Badge>;
  }

  return (
    <main className="flex flex-1 items-center justify-center p-8">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-2xl">Argosy</CardTitle>
          <CardDescription>
            Multi-agent financial advisor &mdash; Phase 0 scaffold
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">API status:</span>
            {badge}
          </div>
          {details && (
            <pre className="text-xs font-mono bg-muted text-muted-foreground p-3 rounded-md overflow-x-auto">
              {JSON.stringify(details, null, 2)}
            </pre>
          )}
          {state === "error" && !details && (
            <p className="text-sm text-red-600">
              Could not reach the API. Is uvicorn running on port 8000?
            </p>
          )}
        </CardContent>
      </Card>
    </main>
  );
}

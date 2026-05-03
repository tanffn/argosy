"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { api, type AgentActivityRow } from "@/lib/api";

const USER_ID = "ariel";

interface RoleRollup {
  role: string;
  count: number;
  cost_usd: number;
}

function rollup(rows: AgentActivityRow[]): RoleRollup[] {
  const map = new Map<string, RoleRollup>();
  for (const r of rows) {
    const k = r.agent_role;
    const cur = map.get(k) ?? { role: k, count: 0, cost_usd: 0 };
    cur.count += 1;
    cur.cost_usd += r.cost_usd ?? 0;
    map.set(k, cur);
  }
  return Array.from(map.values()).sort((a, b) => b.cost_usd - a.cost_usd);
}

export default function AgentsPage() {
  const [rows, setRows] = useState<AgentActivityRow[]>([]);
  const [selected, setSelected] = useState<AgentActivityRow | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const r = await api.agentActivity(USER_ID, 200);
      setRows(r.rows);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const monthlyRollup = useMemo(() => rollup(rows), [rows]);
  const monthlyTotal = useMemo(
    () => monthlyRollup.reduce((acc, r) => acc + r.cost_usd, 0),
    [monthlyRollup],
  );

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            Agent activity
          </h1>
          <p className="text-sm text-muted-foreground">
            Live timeline of agent invocations. Click a row for full prompt /
            response detail.
          </p>
        </div>
        <Button onClick={refresh} variant="outline" size="sm">
          Refresh
        </Button>
      </header>

      {loading && <p className="text-sm text-muted-foreground">Loading...</p>}
      {error && <p className="text-sm text-red-500 font-mono">{error}</p>}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Per-agent rollup ({rows.length} runs in window)
          </CardTitle>
          <CardDescription>
            Total Claude spend in window:{" "}
            <span className="font-mono">${monthlyTotal.toFixed(4)}</span>
          </CardDescription>
        </CardHeader>
        <CardContent>
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-left text-xs text-muted-foreground border-b border-border">
                <th className="py-2">Agent role</th>
                <th className="py-2 text-right">Runs</th>
                <th className="py-2 text-right">Cost (USD)</th>
              </tr>
            </thead>
            <tbody>
              {monthlyRollup.map((r) => (
                <tr key={r.role} className="border-b border-border/40">
                  <td className="py-1.5">{r.role}</td>
                  <td className="py-1.5 text-right">{r.count}</td>
                  <td className="py-1.5 text-right">${r.cost_usd.toFixed(4)}</td>
                </tr>
              ))}
              {monthlyRollup.length === 0 && (
                <tr>
                  <td className="py-3 text-muted-foreground" colSpan={3}>
                    No agent runs yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Recent runs</CardTitle>
          <CardDescription>Click a row to see prompt + response.</CardDescription>
        </CardHeader>
        <CardContent>
          <table className="w-full text-sm font-mono">
            <thead>
              <tr className="text-left text-xs text-muted-foreground border-b border-border">
                <th className="py-2">When</th>
                <th className="py-2">Role</th>
                <th className="py-2">Model</th>
                <th className="py-2">Conf</th>
                <th className="py-2 text-right">In/Out</th>
                <th className="py-2 text-right">Cost</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => setSelected(r)}
                  className="border-b border-border/40 hover:bg-secondary/40 cursor-pointer"
                >
                  <td className="py-1.5">{r.created_at.replace("T", " ").slice(0, 19)}</td>
                  <td className="py-1.5">{r.agent_role}</td>
                  <td className="py-1.5 text-muted-foreground">{r.model}</td>
                  <td className="py-1.5">{r.confidence ?? "-"}</td>
                  <td className="py-1.5 text-right">
                    {r.tokens_in}/{r.tokens_out}
                  </td>
                  <td className="py-1.5 text-right">${r.cost_usd.toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {selected && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <CardTitle className="text-base">
                Run #{selected.id} — {selected.agent_role}
              </CardTitle>
              <Button
                onClick={() => setSelected(null)}
                size="sm"
                variant="ghost"
              >
                Close
              </Button>
            </div>
            <CardDescription>
              {selected.created_at} · {selected.model} ·{" "}
              {selected.confidence ?? "no-confidence"} · ${selected.cost_usd.toFixed(4)}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground mb-2">
              decision_id: {selected.decision_id ?? "(none)"}
            </p>
            <p className="text-sm">
              Full prompt / response detail is recorded in `agent_reports.response_text`
              and the audit log. Open the audit screen to drill in further.
            </p>
          </CardContent>
        </Card>
      )}
    </main>
  );
}

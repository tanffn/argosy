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
import { api, type AuditItem } from "@/lib/api";

const USER_ID = "ariel";

export default function AuditPage() {
  const [rows, setRows] = useState<AuditItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [eventType, setEventType] = useState<string>("");
  const [entityType, setEntityType] = useState<string>("");
  const [since, setSince] = useState<string>("");
  const [until, setUntil] = useState<string>("");

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const r = await api.auditList(USER_ID, {
        eventType: eventType || undefined,
        entityType: entityType || undefined,
        since: since || undefined,
        until: until || undefined,
        limit: 200,
      });
      setRows(r.rows);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [eventType, entityType, since, until]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount; refresh() sets local state from the API
    refresh();
  }, [refresh]);

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Audit log</h1>
        <p className="text-sm text-muted-foreground">
          Append-only record of every fill, approval, override, and broker
          interaction. Filter by event type, entity, or date range.
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Filters</CardTitle>
          <CardDescription>
            Empty fields = no filter. Date inputs are ISO datetimes.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid grid-cols-2 md:grid-cols-4 gap-3 items-end">
          <label className="flex flex-col text-xs gap-1">
            <span className="text-muted-foreground">Event type</span>
            <input
              className="bg-background border border-border rounded-md px-3 py-1.5 text-sm font-mono"
              value={eventType}
              onChange={(e) => setEventType(e.target.value)}
              placeholder="e.g. fill.received"
            />
          </label>
          <label className="flex flex-col text-xs gap-1">
            <span className="text-muted-foreground">Entity type</span>
            <input
              className="bg-background border border-border rounded-md px-3 py-1.5 text-sm font-mono"
              value={entityType}
              onChange={(e) => setEntityType(e.target.value)}
              placeholder="e.g. proposal"
            />
          </label>
          <label className="flex flex-col text-xs gap-1">
            <span className="text-muted-foreground">Since (ISO)</span>
            <input
              className="bg-background border border-border rounded-md px-3 py-1.5 text-sm font-mono"
              value={since}
              onChange={(e) => setSince(e.target.value)}
              placeholder="2026-05-01T00:00:00"
            />
          </label>
          <label className="flex flex-col text-xs gap-1">
            <span className="text-muted-foreground">Until (ISO)</span>
            <input
              className="bg-background border border-border rounded-md px-3 py-1.5 text-sm font-mono"
              value={until}
              onChange={(e) => setUntil(e.target.value)}
              placeholder="2026-05-15T00:00:00"
            />
          </label>
          <div className="col-span-2 md:col-span-4 flex justify-end">
            <Button size="sm" onClick={refresh}>
              Refresh
            </Button>
          </div>
        </CardContent>
      </Card>

      {error && <p className="text-sm text-error font-mono">{error}</p>}
      {loading && <p className="text-sm text-muted-foreground">Loading...</p>}

      {!loading && rows.length === 0 && (
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            No audit rows match. Approve or execute a proposal to populate
            the log.
          </CardContent>
        </Card>
      )}

      {!loading && rows.length > 0 && (
        <Card>
          <CardContent className="p-0">
            <table className="w-full text-xs font-mono">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th className="text-left py-2 px-3">when</th>
                  <th className="text-left py-2 px-3">event</th>
                  <th className="text-left py-2 px-3">entity</th>
                  <th className="text-left py-2 px-3">payload</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className="border-b border-border/40 align-top">
                    <td className="py-2 px-3 whitespace-nowrap">{r.created_at}</td>
                    <td className="py-2 px-3 whitespace-nowrap">{r.event_type}</td>
                    <td className="py-2 px-3 whitespace-nowrap">
                      {r.entity_type}/{r.entity_id || "(none)"}
                    </td>
                    <td className="py-2 px-3 break-all max-w-prose">
                      {r.payload_json}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}
    </main>
  );
}

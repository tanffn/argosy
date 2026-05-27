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
import { api } from "@/lib/api";

const USER_ID = "ariel";

type SettingsShape = Record<string, unknown>;

export default function SettingsPage() {
  const [settings, setSettings] = useState<SettingsShape | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      const s = await api.getAgentSettings(USER_ID);
      setSettings(s);
      setDraft(JSON.stringify(s, null, 2));
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount; refresh() sets local state from the API
    refresh();
  }, [refresh]);

  const save = async () => {
    try {
      setError(null);
      let parsed: SettingsShape;
      try {
        parsed = JSON.parse(draft);
      } catch (e) {
        setError(`Invalid JSON: ${e}`);
        return;
      }
      const updated = await api.patchAgentSettings(USER_ID, parsed);
      setSettings(updated);
      setDraft(JSON.stringify(updated, null, 2));
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setConfirmOpen(false);
    }
  };

  return (
    <main className="max-w-4xl mx-auto p-6 flex flex-col gap-6">
      <header className="flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
          <p className="text-sm text-muted-foreground">
            Cadence scheduling, tier thresholds, model overrides, alert
            channels, and backup destination. Edits go through a confirmation
            modal — some changes require an engine restart.
          </p>
        </div>
        <Button onClick={refresh} variant="outline" size="sm">
          Reload
        </Button>
      </header>

      {loading && <p className="text-sm text-muted-foreground">Loading...</p>}
      {error && <p className="text-sm text-error font-mono">{error}</p>}
      {saved && <p className="text-sm text-success">Saved.</p>}

      {settings && (
        <>
          <SectionPreview
            title="Cadences"
            description="Per-loop enable + cron / interval. Restart required to take effect."
            data={settings.cadences}
          />
          <SectionPreview
            title="Tiers"
            description="T0/T1/T2 max-pct thresholds; cooling-off; account-scoped escalation."
            data={settings.tiers}
          />
          <SectionPreview
            title="Execution"
            description="Default execution mode (paper / live / queue_only)."
            data={settings.execution}
          />
          <SectionPreview
            title="Models"
            description="Per-role model defaults + overrides."
            data={settings.models}
          />
          <SectionPreview
            title="Limited account"
            description="Argonaut size + execution mode + per-decision caps."
            data={settings.limited_account}
          />
          <SectionPreview
            title="Cost"
            description="Monthly Claude budget + alert/pause thresholds."
            data={settings.cost}
          />
          <SectionPreview
            title="Backups"
            description="Backup directory, off-site path, retention windows."
            data={settings.backups}
          />
          <SectionPreview
            title="Alerts"
            description="Email + telegram (placeholder) channels."
            data={settings.alerts}
          />

          <Card>
            <CardHeader>
              <CardTitle className="text-base">Edit raw JSON</CardTitle>
              <CardDescription>
                Patch is deep-merged; only fields you change need to be present.
                Hit Save to apply.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <textarea
                className="bg-background border border-border rounded-md px-3 py-2 text-xs font-mono min-h-[300px]"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
              <div className="flex items-center gap-2">
                <Button onClick={() => setConfirmOpen(true)} size="sm">
                  Save
                </Button>
                <span className="text-xs text-muted-foreground">
                  Some changes require an engine restart.
                </span>
              </div>
            </CardContent>
          </Card>
        </>
      )}

      {confirmOpen && (
        <div className="fixed inset-0 bg-background/80 backdrop-blur flex items-center justify-center z-20">
          <Card className="max-w-md">
            <CardHeader>
              <CardTitle className="text-base">Confirm save</CardTitle>
              <CardDescription>
                This writes agent_settings.yaml. Field-level validation runs
                server-side; if anything fails you&apos;ll see an error.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex gap-2 justify-end">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setConfirmOpen(false)}
              >
                Cancel
              </Button>
              <Button size="sm" onClick={save}>
                Save
              </Button>
            </CardContent>
          </Card>
        </div>
      )}
    </main>
  );
}

function SectionPreview({
  title,
  description,
  data,
}: {
  title: string;
  description: string;
  data: unknown;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <pre className="text-xs font-mono whitespace-pre-wrap text-muted-foreground">
          {JSON.stringify(data, null, 2)}
        </pre>
      </CardContent>
    </Card>
  );
}

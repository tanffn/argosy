/**
 * /settings/notifications — Spec E commit #7 / spec §6.2.
 *
 * Renders the push-subscription opt-in card (PushSubscriptionCard) +
 * the channel x severity x kind preference matrix. The matrix is a
 * compact grid: one section per channel, severities as rows, kinds as
 * columns, each cell a checkbox. Default opt-in per spec §3.3 — missing
 * rows materialise as enabled, the user mutes by un-checking.
 *
 * Sibling routes:
 *   - /settings  -> existing agent_settings JSON editor.
 *   - /proposals -> open action_proposals queue (sibling commit #6).
 */
"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { PushSubscriptionCard } from "@/components/notifications/PushSubscriptionCard";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  api,
  type NotificationPreferenceCell,
  type NotificationPreferencesResponse,
} from "@/lib/api";

const USER_ID = "ariel";

type CellKey = `${string}|${string}|${string}`;

function cellKey(channel: string, severity: string, kind: string): CellKey {
  return `${channel}|${severity}|${kind}` as CellKey;
}

/**
 * Pretty-print a kind enum value for the matrix column header. We keep
 * snake_case in flight (matches the backend wire shape) but render
 * "Repatriate currency" etc. for the human eye.
 */
function formatKind(kind: string): string {
  return kind
    .split("_")
    .map((part, idx) =>
      idx === 0 ? part.charAt(0).toUpperCase() + part.slice(1) : part,
    )
    .join(" ");
}

function formatChannel(channel: string): string {
  switch (channel) {
    case "in_app":
      return "In-app";
    case "web_push":
      return "Web push";
    case "email":
      return "Email";
    default:
      return channel;
  }
}

export default function NotificationsSettingsPage() {
  const [matrix, setMatrix] = useState<NotificationPreferencesResponse | null>(
    null,
  );
  const [pending, setPending] = useState<Map<CellKey, NotificationPreferenceCell>>(
    new Map(),
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      setLoading(true);
      setError(null);
      const res = await api.notifications.listPreferences(USER_ID);
      setMatrix(res);
      setPending(new Map());
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- fetch-on-mount; refresh() sets local state from the API
    refresh();
  }, [refresh]);

  // Materialise the stored matrix into a (key -> enabled) lookup so the
  // checkboxes don't have to .find() through the cells array on every
  // render.
  const storedLookup = useMemo(() => {
    const map = new Map<CellKey, boolean>();
    if (!matrix) return map;
    for (const cell of matrix.cells) {
      map.set(cellKey(cell.channel, cell.severity, cell.kind), cell.enabled);
    }
    return map;
  }, [matrix]);

  const cellChecked = (
    channel: string,
    severity: string,
    kind: string,
  ): boolean => {
    const key = cellKey(channel, severity, kind);
    if (pending.has(key)) {
      return pending.get(key)!.enabled;
    }
    if (storedLookup.has(key)) {
      return storedLookup.get(key)!;
    }
    // Spec §3.3 default-on.
    return true;
  };

  const toggleCell = (
    channel: string,
    severity: string,
    kind: string,
    next: boolean,
  ) => {
    setSavedAt(null);
    const key = cellKey(channel, severity, kind);
    const stored = storedLookup.get(key) ?? true;
    setPending((prev) => {
      const copy = new Map(prev);
      if (stored === next) {
        // Reverting to the stored value — drop the pending edit.
        copy.delete(key);
      } else {
        copy.set(key, {
          channel: channel as NotificationPreferenceCell["channel"],
          severity: severity as NotificationPreferenceCell["severity"],
          kind,
          enabled: next,
        });
      }
      return copy;
    });
  };

  const save = async () => {
    if (pending.size === 0) return;
    setSaving(true);
    setError(null);
    try {
      const cells = Array.from(pending.values());
      const next = await api.notifications.updatePreferences(USER_ID, cells);
      setMatrix(next);
      setPending(new Map());
      setSavedAt(Date.now());
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  };

  const discardChanges = () => {
    setPending(new Map());
    setSavedAt(null);
  };

  return (
    <main className="max-w-6xl mx-auto p-6 flex flex-col gap-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          Notification settings
        </h1>
        <p className="text-sm text-muted-foreground">
          Opt this browser into web push and tune which channels fire for
          each (severity, kind) cell. Defaults are opt-in everywhere — the
          dispatcher only mutes a cell when you explicitly uncheck it.
        </p>
      </header>

      <PushSubscriptionCard userId={USER_ID} />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Channel x severity x kind matrix
          </CardTitle>
          <CardDescription>
            One block per channel. Rows are severity bands; columns are the
            eight action-proposal kinds. Uncheck a cell to mute that
            (channel, severity, kind) combination. The dispatcher
            re-evaluates this matrix on every notification, so changes
            take effect immediately on save.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {loading && (
            <p className="text-sm text-muted-foreground">Loading matrix…</p>
          )}
          {error && (
            <p className="text-sm text-error font-mono">{error}</p>
          )}

          {matrix && (
            <>
              {matrix.channels.map((channel) => (
                <ChannelMatrix
                  key={channel}
                  channel={channel}
                  severities={matrix.severities}
                  kinds={matrix.kinds}
                  cellChecked={cellChecked}
                  onToggle={toggleCell}
                />
              ))}

              <div className="flex items-center gap-3 pt-2">
                <Button
                  size="sm"
                  onClick={save}
                  disabled={saving || pending.size === 0}
                >
                  {saving
                    ? "Saving…"
                    : pending.size === 0
                      ? "No changes"
                      : `Save ${pending.size} change${pending.size === 1 ? "" : "s"}`}
                </Button>
                {pending.size > 0 && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={discardChanges}
                    disabled={saving}
                  >
                    Discard
                  </Button>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={refresh}
                  disabled={saving || loading}
                >
                  Reload
                </Button>
                {savedAt !== null && (
                  <span className="text-xs text-success">
                    Saved at {new Date(savedAt).toLocaleTimeString()}.
                  </span>
                )}
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </main>
  );
}

interface ChannelMatrixProps {
  channel: string;
  severities: string[];
  kinds: string[];
  cellChecked: (channel: string, severity: string, kind: string) => boolean;
  onToggle: (
    channel: string,
    severity: string,
    kind: string,
    next: boolean,
  ) => void;
}

function ChannelMatrix({
  channel,
  severities,
  kinds,
  cellChecked,
  onToggle,
}: ChannelMatrixProps) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-sm font-semibold text-foreground">
        {formatChannel(channel)}
      </h2>
      <div className="overflow-x-auto rounded-md border border-border">
        <table className="min-w-full text-xs">
          <thead className="bg-secondary/40">
            <tr>
              <th className="text-left px-3 py-2 font-medium text-muted-foreground">
                Severity
              </th>
              {kinds.map((kind) => (
                <th
                  key={kind}
                  className="text-left px-3 py-2 font-medium text-muted-foreground"
                >
                  {formatKind(kind)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {severities.map((severity) => (
              <tr key={severity} className="border-t border-border">
                <td className="px-3 py-2 font-mono text-muted-foreground">
                  {severity}
                </td>
                {kinds.map((kind) => {
                  const checked = cellChecked(channel, severity, kind);
                  const id = `pref-${channel}-${severity}-${kind}`;
                  return (
                    <td key={kind} className="px-3 py-2">
                      <Checkbox
                        id={id}
                        checked={checked}
                        onCheckedChange={(next) =>
                          onToggle(channel, severity, kind, next)
                        }
                      />
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

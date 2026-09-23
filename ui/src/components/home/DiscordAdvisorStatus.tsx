"use client";

import { useHomeRead } from "./use-home-read";

type AdvisorStatus = { status: string; message: string; attention_required: boolean };

async function loadStatus(userId: string, signal: AbortSignal): Promise<AdvisorStatus> {
  const response = await fetch(`/api/health/discord-advisor?user_id=${encodeURIComponent(userId)}`, {
    cache: "no-store", signal,
  });
  if (!response.ok) throw new Error("Discord status unavailable");
  const value: AdvisorStatus = await response.json();
  if (typeof value.status !== "string" || typeof value.message !== "string" || typeof value.attention_required !== "boolean") {
    throw new Error("Invalid Discord status");
  }
  return value;
}

export function DiscordAdvisorStatus({ userId }: { userId: string }) {
  const { data, failed, retry } = useHomeRead(loadStatus, userId);
  if (!data && !failed) return null;
  const urgent = data?.attention_required === true;
  return (
    <aside aria-label="Private Discord advisor" role={urgent ? "alert" : "status"}
      className={`rounded-lg border px-4 py-3 text-sm ${urgent ? "border-red-500/40 bg-red-500/10" : "border-border text-muted-foreground"}`}>
      <strong>Discord advisor:</strong>{" "}
      {failed ? "Connection status unavailable; this is not evidence the bot is connected." : data?.message}
      {failed && <button onClick={retry} className="ml-2 underline">Retry</button>}
    </aside>
  );
}

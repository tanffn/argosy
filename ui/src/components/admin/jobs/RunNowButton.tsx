"use client";

// Sprint A commit #8 — RunNow / Stop / Reconnect buttons.
//
// CadenceLoop:  one "Run now" button. POST /api/jobs/{name}/run-now.
//   On success: optimistic-update the row to status='running', then
//   poll /api/jobs/{name} every 2 s until last_run_status != 'running'.
//   On 409: surface "already running" inline + the linked job_run_id.
//
// LongRunningJob: "Reconnect" + "Stop" buttons. Reconnect = stop + run-now
//   in one call (POST /reconnect). Stop = POST /stop.
//
// 401 from the server surfaces as "admin token rejected — clear + paste
// a fresh one". Other errors surface as a small red banner under the
// button group.

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { api, JobApiError, type JobConflictBody, type JobView } from "@/lib/api";

interface Props {
  job: JobView;
  /** Called when a successful action completes so the parent can refresh. */
  onChanged: () => void;
}

const POLL_INTERVAL_MS = 2000;
const POLL_MAX_TICKS = 90; // ~3 minutes; sufficient for news_daily 60-90s ticks

export function RunNowButton({ job, onChanged }: Props) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<JobConflictBody | null>(null);
  const pollHandle = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (pollHandle.current) clearTimeout(pollHandle.current);
    };
  }, []);

  const pollUntilDone = (name: string, ticks: number) => {
    pollHandle.current = setTimeout(async () => {
      try {
        const detail = await api.jobs.get(name);
        const stillRunning =
          detail.view.last_run_status === "running" ||
          detail.view.currently_running_run_id !== null;
        onChanged();
        if (stillRunning && ticks > 0) {
          pollUntilDone(name, ticks - 1);
        } else {
          setPending(false);
        }
      } catch {
        // Transient fetch failure: stop polling but leave the row in
        // whatever state the parent's polling next surfaces.
        setPending(false);
      }
    }, POLL_INTERVAL_MS);
  };

  const handleRunNow = async () => {
    setError(null);
    setConflict(null);
    setPending(true);
    try {
      await api.jobs.runNow(job.metadata.name);
      onChanged(); // optimistic refresh; the row should now show 'running'
      pollUntilDone(job.metadata.name, POLL_MAX_TICKS);
    } catch (e) {
      setPending(false);
      if (e instanceof JobApiError) {
        if (e.status === 409 && e.body && typeof e.body === "object") {
          setConflict(e.body as JobConflictBody);
          return;
        }
        if (e.status === 401) {
          setError("Admin token rejected — clear it and paste a fresh one.");
          return;
        }
      }
      setError(String(e instanceof Error ? e.message : e));
    }
  };

  const handleStop = async () => {
    setError(null);
    setPending(true);
    try {
      await api.jobs.stop(job.metadata.name);
      onChanged();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    } finally {
      setPending(false);
    }
  };

  const handleReconnect = async () => {
    setError(null);
    setConflict(null);
    setPending(true);
    try {
      await api.jobs.reconnect(job.metadata.name);
      onChanged();
      pollUntilDone(job.metadata.name, POLL_MAX_TICKS);
    } catch (e) {
      setPending(false);
      if (e instanceof JobApiError && e.status === 401) {
        setError("Admin token rejected — clear it and paste a fresh one.");
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="flex flex-col gap-1 items-end">
      <div className="flex items-center gap-1.5">
        {job.metadata.long_running ? (
          <>
            <Button
              size="sm"
              variant="outline"
              onClick={handleReconnect}
              disabled={pending}
            >
              {pending ? "Working…" : "Reconnect"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={handleStop}
              disabled={pending}
            >
              Stop
            </Button>
          </>
        ) : (
          <Button
            size="sm"
            variant="outline"
            onClick={handleRunNow}
            disabled={pending}
          >
            {pending ? "Running…" : "Run now"}
          </Button>
        )}
      </div>
      {conflict && (
        <span className="text-[11px] text-warning">
          Already running
          {conflict.job_run_id !== null
            ? ` — run #${conflict.job_run_id}, view history`
            : " — view history"}
          .
        </span>
      )}
      {error && (
        <span className="text-[11px] text-error max-w-[200px] text-right">
          {error}
        </span>
      )}
    </div>
  );
}

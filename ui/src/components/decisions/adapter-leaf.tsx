import { AlertCircle, CheckCircle2, MinusCircle } from "lucide-react";

import type { AdapterNode } from "@/lib/api";

// T0.6 — leaf row for one adapter call (e.g. finnhub_news, yfinance,
// fred, boi). Rendered as a child of the analyst node that consumed it
// in <AgentTree>. Status colors come from the shared Tailwind tokens
// (text-success / text-warning / text-error).
export function AdapterLeaf({ adapter }: { adapter: AdapterNode }) {
  const Icon =
    adapter.status === "ok"
      ? CheckCircle2
      : adapter.status === "empty"
        ? MinusCircle
        : AlertCircle;
  const color =
    adapter.status === "ok"
      ? "text-success"
      : adapter.status === "empty"
        ? "text-warning"
        : "text-error";
  return (
    <div className="flex items-center gap-2 px-2 py-1 text-[11px]">
      <Icon
        className={`h-3 w-3 shrink-0 ${color}`}
        aria-hidden
        suppressHydrationWarning
      />
      <span className="font-mono">{adapter.adapter_name}</span>
      {adapter.target && (
        <span className="text-muted-foreground">{adapter.target}</span>
      )}
      <span className="text-muted-foreground">{adapter.latency_ms}ms</span>
      <span className="text-muted-foreground">
        {adapter.payload_size_bytes}B
      </span>
      {adapter.http_status_code !== null && adapter.status !== "ok" && (
        <span className="text-error">HTTP {adapter.http_status_code}</span>
      )}
      {adapter.error_text && (
        <span
          className="text-error truncate max-w-md"
          title={adapter.error_text}
        >
          {adapter.error_text}
        </span>
      )}
    </div>
  );
}

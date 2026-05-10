"use client";

import { type MonthlyTotalEntry } from "@/lib/expenses/api";
import { formatMonth } from "@/lib/expenses/format";

interface MonthPickerProps {
  /** Months from the dashboard payload — oldest → newest. */
  months: MonthlyTotalEntry[];
  /** Currently-picked month (`YYYY-MM`) or null = latest available. */
  value: string | null;
  /** Called with the new month, or `null` to mean "back to latest". */
  onChange: (month: string | null) => void;
}

/**
 * Plain `<select>` styled like the rest of the filters. Renders every month
 * in `months` newest-first; the latest is the implicit default ("Latest"
 * chip points at it). Picking a value updates URL and refetches dashboard.
 */
export function MonthPicker({ months, value, onChange }: MonthPickerProps) {
  // Newest-first order — what the user expects when scanning a dropdown.
  const ordered = [...months].sort((a, b) => b.month.localeCompare(a.month));
  const latest = ordered[0]?.month ?? null;
  const effective = value ?? latest;

  return (
    <label className="inline-flex items-center gap-2 text-sm">
      <span className="text-muted-foreground text-xs">Month</span>
      <select
        value={effective ?? ""}
        onChange={(e) => {
          const next = e.target.value;
          // Picking the latest month → clear the URL param so it tracks
          // "latest" implicitly.
          if (next === latest) onChange(null);
          else onChange(next);
        }}
        className="bg-background border border-border rounded px-2 py-1.5 text-sm"
        disabled={ordered.length === 0}
      >
        {ordered.length === 0 ? (
          <option value="">No months</option>
        ) : (
          ordered.map((m) => (
            <option key={m.month} value={m.month}>
              {formatMonth(m.month)}
              {m.month === latest ? " (latest)" : ""}
            </option>
          ))
        )}
      </select>
    </label>
  );
}

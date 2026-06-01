import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Format an ISO-8601 timestamp as `YYYY-MM-DD HH:mm` in the user's
 * local timezone. The backend always emits UTC-tagged ISO strings
 * (see `_iso_utc` in argosy/api/routes/plan.py); JavaScript's `Date`
 * constructor converts to local on parse. Returns `""` for null /
 * unparseable input so call sites can string-concatenate safely.
 *
 * Centralized so every plan/draft/decision date renders the same way
 * — replaces ad-hoc `toLocaleString()` / `toLocaleDateString()` calls
 * that produced locale-dependent MM/DD/YYYY or DD.MM.YYYY surfaces.
 */
export function formatLocalDateTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => n.toString().padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

/** Same as formatLocalDateTime but date-only (YYYY-MM-DD). */
export function formatLocalDate(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

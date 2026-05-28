/**
 * Base URL helper for direct fetch() calls. Most callers use the `api`
 * namespace from ``@/lib/api`` which wraps this; export it separately
 * for components that need to fetch with custom plumbing (e.g. recharts
 * data feeds where we want a thin response shape).
 */
export function apiUrl(path: string): string {
  const base =
    typeof process !== "undefined" && process.env.NEXT_PUBLIC_API_URL
      ? process.env.NEXT_PUBLIC_API_URL
      : "http://localhost:8000";
  return path.startsWith("http") ? path : `${base}${path}`;
}

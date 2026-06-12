import { defineConfig } from "vitest/config";
import path from "node:path";

/**
 * Vitest config — T5.4 frontend test scaffold.
 *
 * Path aliases mirror ``ui/tsconfig.json``: ``@/*`` → ``./src/*``.
 *
 * jsdom environment is required for React component / hook tests so that
 * ``@testing-library/react``'s ``renderHook`` and friends have a DOM
 * to render against. CLI / pure-logic tests can override per-file with
 * a ``// @vitest-environment node`` comment if needed.
 *
 * ``@testing-library/jest-dom`` matchers (``toBeInTheDocument``, etc.) are
 * wired via ``setupFiles`` so they are available globally to all component
 * tests without a per-file import.
 */
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/__tests__/**/*.{test,spec}.{ts,tsx}"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});

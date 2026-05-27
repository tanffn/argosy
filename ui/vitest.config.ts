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
 * No setupFiles wired yet — when we want global ``@testing-library/jest-dom``
 * matchers we'll add ``setupFiles: ["./vitest.setup.ts"]`` here and create
 * that file with ``import "@testing-library/jest-dom/vitest"``.
 */
export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/__tests__/**/*.{test,spec}.{ts,tsx}"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});

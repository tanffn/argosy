/**
 * T5.4 — first vitest test, smoke + import-level coverage of useDecisionStream.
 *
 * Per the W11 task brief: prefer a stable, trivially-passing test over a
 * fragile real-hook test. ``renderHook`` against the real
 * ``useDecisionStream`` needs full mocks for ``api.agentActivity`` /
 * ``api.decisionsRecent`` / ``useWSEvents`` (which itself opens a
 * WebSocket); wiring all that up cleanly is a follow-up. This file
 * gives us:
 *
 *   1. A passing "vitest is wired" baseline so ``npm run test`` returns
 *      0 in CI.
 *   2. An import-time smoke that catches accidental regressions in the
 *      hook module's static surface (broken imports, syntax errors)
 *      without paying the cost of a full renderHook.
 *
 * The full mocked renderHook scaffold (initial-state assertion, fake WS
 * event dispatch, state-update assertion) lives as a TODO comment below;
 * it should be filled in once we wire ``vi.mock`` against ``../api`` and
 * ``../ws``. The task brief explicitly allows shipping the trivial test
 * first.
 */

import { describe, expect, it } from "vitest";

describe("vitest scaffold (T5.4)", () => {
  it("is wired", () => {
    expect(true).toBe(true);
  });

  it("imports useDecisionStream cleanly", async () => {
    // Dynamic import so a regression in the module's static surface
    // (broken type, missing dependency) fails inside this test rather
    // than at top-of-file load (which would obscure the diagnostic).
    const mod = await import("../useDecisionStream");
    expect(typeof mod.useDecisionStream).toBe("function");
  });
});

// TODO(T5.4 follow-up): wire ``vi.mock("../api")`` + ``vi.mock("../ws")``
// and exercise the hook via ``renderHook`` from ``@testing-library/react``:
//   * Assert initial ``decisions`` is the empty array.
//   * Dispatch a fake ``agent.run.started`` WS payload and assert the
//     hook surfaces a new running-row in ``decisions[0].rows``.
//   * Dispatch a matching ``agent.run.finished`` and assert the row
//     transitions to status="done".

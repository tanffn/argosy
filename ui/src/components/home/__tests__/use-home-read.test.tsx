import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { useHomeRead } from "../use-home-read";

afterEach(() => { vi.useRealTimers(); });

it("times out a stalled read and reports unavailable", async () => {
  vi.useFakeTimers();
  const load = vi.fn((_user: string, signal: AbortSignal) => new Promise<string>((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
  }));
  const { result } = renderHook(() => useHomeRead(load, "ariel"));
  await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
  expect(result.current.failed).toBe(true);
  expect(result.current.data).toBeNull();
});

it("refreshes every minute and does not overlap a pending read", async () => {
  vi.useFakeTimers();
  let finish: (value: string) => void = () => {};
  const load = vi.fn(() => new Promise<string>((resolve) => { finish = resolve; }));
  const { result } = renderHook(() => useHomeRead(load, "ariel"));
  await act(async () => { window.dispatchEvent(new Event("focus")); });
  expect(load).toHaveBeenCalledTimes(1);
  await act(async () => { finish("first"); });
  expect(result.current.data).toBe("first");
  await act(async () => { await vi.advanceTimersByTimeAsync(60000); });
  expect(load).toHaveBeenCalledTimes(2);
  await act(async () => { finish("second"); });
  expect(result.current.data).toBe("second");
});

it("never exposes the previous user's data when the user changes", async () => {
  const load = vi.fn((user: string) => user === "a" ? Promise.resolve("private a") : new Promise<string>(() => {}));
  const { result, rerender } = renderHook(({ user }) => useHomeRead(load, user), { initialProps: { user: "a" } });
  await act(async () => {});
  expect(result.current.data).toBe("private a");
  rerender({ user: "b" });
  expect(result.current.data).toBeNull();
});

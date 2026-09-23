import { afterEach, expect, it, vi } from "vitest";
import { api } from "../api";

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("routes browser dashboard reads through the UI origin, not the browser's localhost", async () => {
  vi.stubEnv("NEXT_PUBLIC_API_URL", "");
  vi.spyOn(window, "location", "get").mockReturnValue(new URL("http://argosy.example:1337") as unknown as Location);
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
  vi.stubGlobal("fetch", fetcher);
  await api.getInbox("ariel", true);
  await api.homeGreeting("ariel");
  expect(fetcher.mock.calls[0][0]).toBe("http://argosy.example:1337/api/inbox?user_id=ariel&debug=true");
  expect(fetcher.mock.calls[1][0]).toBe("http://argosy.example:1337/api/home/greeting?user_id=ariel");
});

it("preserves an explicitly configured API host", async () => {
  vi.stubEnv("NEXT_PUBLIC_API_URL", "https://api.argosy.example");
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response("{}", { status: 200 })));
  vi.stubGlobal("fetch", fetcher);
  await api.getInbox("ariel");
  expect(fetcher.mock.calls[0][0]).toBe("https://api.argosy.example/api/inbox?user_id=ariel");
});

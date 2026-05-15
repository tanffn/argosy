// Onboarding flow (Phase 6).
//
// Accepts a setup token (from the URL `?token=` or pasted manually)
// and walks the new tenant through the Phase 1 intake re-skinned for
// first-time use. At the end the dashboard becomes accessible.

"use client";

import { useState } from "react";

export default function OnboardingPage() {
  const [token, setToken] = useState<string>("");
  const [email, setEmail] = useState<string>("");
  const [status, setStatus] = useState<"idle" | "submitting" | "ok" | "error">(
    "idle"
  );
  const [errorMsg, setErrorMsg] = useState<string>("");

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setStatus("submitting");
    setErrorMsg("");
    try {
      // Sign in via NextAuth credentials provider.
      const res = await fetch("/api/auth/callback/credentials", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          email,
          token,
          callbackUrl: "/",
          json: "true",
        }),
      });
      if (!res.ok) {
        setStatus("error");
        setErrorMsg("Invalid setup token or email");
        return;
      }
      setStatus("ok");
      window.location.href = "/";
    } catch (e) {
      setStatus("error");
      setErrorMsg(String(e));
    }
  }

  return (
    <div className="mx-auto mt-16 max-w-md p-6">
      <h1 className="text-2xl font-semibold mb-2">Welcome to Argosy</h1>
      <p className="text-sm text-muted-foreground mb-6">
        Enter the setup token your administrator sent you. After
        first-login Argosy will walk you through plan intake.
      </p>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <label className="flex flex-col gap-1">
          <span className="text-sm">Email</span>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="border rounded px-3 py-2"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-sm">Setup token</span>
          <input
            type="text"
            required
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="border rounded px-3 py-2"
          />
        </label>
        <button
          type="submit"
          disabled={status === "submitting"}
          className="rounded bg-primary text-primary-foreground px-4 py-2 disabled:opacity-50"
        >
          {status === "submitting" ? "Verifying..." : "Continue"}
        </button>
        {errorMsg && <p className="text-sm text-error">{errorMsg}</p>}
      </form>
    </div>
  );
}

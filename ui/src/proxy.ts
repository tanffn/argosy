// Sprint A commit #8 — Next.js Proxy (formerly middleware.ts in <=15;
// renamed to `proxy.ts` in Next.js 16). See
// node_modules/next/dist/docs/01-app/02-guides/upgrading/version-16.md
// (`middleware` to `proxy`).
//
// Scope: gates `/admin/*` routes. Because the admin token lives in
// localStorage (single-user system; see SDD §commit #4 + #8) and the
// proxy runs server-side with no access to localStorage, the *actual*
// gate is the <AdminTokenGate /> client component rendered by
// `app/admin/jobs/page.tsx`. This proxy is the structural seam:
//
//   - It matches `/admin/*` paths and lets them through to the page,
//     which then renders AdminTokenGate when the token is absent.
//   - A future cookie-based token (mirroring localStorage on save) can
//     be checked here to short-circuit to a server-side redirect; left
//     as a TODO so the v1 single-user flow stays simple.
//
// `runtime` is unconfigurable in Next.js 16 proxy (always nodejs).

import { NextResponse } from "next/server";

export function proxy() {
  // No-op pass-through. Client-side AdminTokenGate handles the actual
  // localStorage check + token-paste UX. Kept as a named export so the
  // matcher below is reachable, and so a future cookie-based check has
  // a single landing spot (read the cookie from `request` then).
  return NextResponse.next();
}

export const config = {
  matcher: ["/admin/:path*"],
};

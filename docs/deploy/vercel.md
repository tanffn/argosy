# Vercel deploy (UI)

Deploy the Next.js dashboard to Vercel. The Argosy engine runs
elsewhere (Fly.io, a VPS, or self-hosted Docker); Vercel only serves
the UI and rewrites `/api/*` to the engine.

## One-time setup

1. Push the repo to a Git remote Vercel can read (GitHub / GitLab).
2. In the Vercel dashboard, **Add Project** and select the `ui/`
   subdirectory as the project root.
3. Set environment variables in the Vercel project settings:

   | Name | Value |
   |---|---|
   | `NEXT_PUBLIC_API_URL` | `https://argosy-engine.fly.dev` (or your engine URL) |
   | `NEXTAUTH_URL` | `https://your-vercel-domain` |
   | `NEXTAUTH_SECRET` | `openssl rand -hex 32` output |

4. The committed `ui/vercel.json` rewrites `/api/(.*)` →
   `${NEXT_PUBLIC_API_URL}/api/$1` and `/internal/*` → engine. The
   `/api/auth/*` path is **not** rewritten — NextAuth routes run on
   Vercel.

## First deploy

```bash
git push
```

Vercel auto-detects Next.js and runs `npm install` + `npm run build`.

## Verify

After deploy succeeds:

1. Visit `https://your-vercel-domain/onboarding`.
2. Paste the setup token your operator generated via
   `argosy admin tenant create`.
3. After redeem, the dashboard loads with per-tenant branding.

## Custom domains

Add a custom domain in the Vercel dashboard. For white-label tenants,
use a wildcard subdomain (`*.argosy.app`) and have your DNS provider
CNAME each tenant subdomain to Vercel.

## Per-tenant theming

`/api/branding?user_id=<id>` returns the tenant's branding YAML. The
`BrandingProvider` in `ui/src/components/branding-provider.tsx` reads
it on mount and applies theme tokens.

# Self-hosted Docker compose

Single-host deploy. Brings up the engine + the Next.js UI on the same
machine; data lives in a named Docker volume.

## Prerequisites

- Docker Engine 24+ with the `compose` plugin.
- 4 GB RAM and ~10 GB disk for moderate use.
- Optional: a TWS Gateway running on the host for live IBKR access
  (left commented out in `docker-compose.yml`).

## First run

```bash
# from the repo root
NEXTAUTH_SECRET=$(openssl rand -hex 32) docker compose up --build -d
```

The committed `docker-compose.yml` builds two images
(`argosy/engine:latest`, `argosy/ui:latest`), creates the
`argosy-home` volume, and exposes:

- `http://localhost:8000` — engine + `/api/*` + `/internal/*`
- `http://localhost:1337` — Next.js dashboard

## Tenant onboarding

```bash
docker compose exec engine argosy admin tenant create \
  --user-id alice --email alice@example.com --plan pro
```

Send the setup token to the new tenant; they go to
`http://your-host:1337/onboarding` and paste it.

## Backups

The `argosy-home` named volume holds:

- Control DB at `/data/db/argosy.db`
- Per-tenant DBs at `/data/tenants/<user_id>/argosy.db`
- Configs at `/data/configs/<user_id>/`
- Backups at `/data/backups/`

Mount the volume read-only into a backup container:

```yaml
services:
  backup:
    image: argosy/engine:latest
    volumes:
      - argosy-home:/data:ro
      - ./backups:/host-backups
    command: ["python", "-m", "argosy.cli.main", "run", "--backup-only"]
```

## Watchdog

Uncomment the `watchdog` service block in `docker-compose.yml` and
restart compose. Email alerts use the existing
`configs/<user_id>/email_settings.yaml` flow from Phase 4.

## Production hardening

- Run behind nginx / Caddy with TLS termination.
- Set `ARGOSY_CORS_ORIGINS` to your hosted domain only.
- Rotate `NEXTAUTH_SECRET` quarterly.
- Restrict `/internal/*` access at the reverse-proxy level (deny by
  default; allow only from the watchdog / ops VPN).

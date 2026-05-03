# Fly.io deploy (engine)

Deploy the Argosy FastAPI engine to a Fly.io app. Each tenant gets a
separate app (one ARGOSY_HOME per tenant) until the engine grows
native multi-DB support — current per-tenant routing assumes a shared
SQLite control plane plus per-tenant data DBs on the same volume.

## One-time setup

1. Install `flyctl` and authenticate.
2. From the repo root, build the engine image and push it to Fly's
   registry (or a public registry):

   ```bash
   docker build -t argosy/engine:latest .
   docker tag argosy/engine:latest registry.fly.io/argosy-engine:latest
   docker push registry.fly.io/argosy-engine:latest
   ```

3. From `deploy/fly/`:

   ```bash
   fly launch --copy-config --no-deploy --name argosy-engine
   fly volumes create argosy_home --size 10 --region iad
   fly secrets set \
     NEXTAUTH_SECRET=$(openssl rand -hex 32) \
     ARGOSY_TENANCY=per-tenant \
     ARGOSY_CORS_ORIGINS=https://your-vercel-domain
   fly deploy
   ```

The committed `deploy/fly/fly.toml` mounts `argosy_home` at `/data`
and runs uvicorn on port 8000 behind Fly's HTTPS handler.

## Tenant onboarding on the deployed engine

Run the admin CLI inside the deployed container:

```bash
fly ssh console -C "argosy admin tenant create --user-id alice --email alice@example.com --plan pro"
```

Capture the printed setup token and send it to the tenant.

## Watchdog as a sidecar

Fly supports multi-process apps. Add a `[processes]` block to
`fly.toml` that runs `argosy admin watchdog start --user-id <id>` for
each tenant:

```toml
[processes]
  app = "uvicorn argosy.api.main:app --host 0.0.0.0 --port 8000"
  watchdog = "argosy admin watchdog start --user-id alice"
```

## Volume backups

```bash
fly ssh sftp shell -a argosy-engine
sftp> get /data/db/argosy.db local-backup.db
```

Daily snapshots run inside the engine via the Phase 4 backup cadence;
the Fly volume is the on-machine store and an off-machine backup
target should be configured separately (e.g., rclone to S3 in a
sidecar).

# Remote Read-Only Demonstration

This Compose project runs only the dashboard Web container. It reads committed
usage snapshots from the production `ai_usage` database through a separate
PostgreSQL role; it does not run Redis, PostgreSQL, mail, sync workers, realtime
workers, billing, organization management, or any upstream management client.

## Prepare the database

1. As a production database owner, review and run
   `readonly_usage_role.sql`. Replace its password placeholder without placing
   it in a shell history or this repository.
2. Verify the role cannot write:

   ```sql
   SET ROLE ai_usage_demo_reader;
   INSERT INTO usage_refresh_requests (request_key, start_date, end_date)
   VALUES ('demo-check', CURRENT_DATE, CURRENT_DATE);
   ```

   PostgreSQL must reject this statement. Roll back the transaction if your
   SQL client opened one.

## Configure and start

1. Copy `.env.remote-demo.example` to `.env.remote-demo` and set a distinct
   session secret, SSO callback configuration, internal bind IP, and read-only
   database URL. Keep all upstream management key variables empty.
2. On `JSZX-AI-03`, deploy from the pushed revision:

   ```bash
   docker compose -p ai-token-dashboard-remote-demo \
     -f docker-compose.remote-demo.yml --env-file .env.remote-demo up -d --build
   ```

3. Put an internal-only reverse-proxy vhost in front of host port `8010` for
   `ai-token-dashboard-dev.internal`; restrict it to the company network or
   VPN. Do not publish this port publicly.

## Verify

```bash
curl -fsS http://127.0.0.1:8010/api/health
docker compose -p ai-token-dashboard-remote-demo -f docker-compose.remote-demo.yml ps
```

`/api/health` reports `remoteDemo.readOnly=true`. The UI hides keys, models,
billing, stability, cost, and governance pages. Requests that would mutate
state return HTTP 403 with `REMOTE_DEMO_READ_ONLY`; `refresh=1` on usage routes
also returns that response and never creates `usage_refresh_requests` rows.

-- Run once against production ai_usage as a database owner, after confirming
-- the production schema has been migrated by the production application.
-- Replace the placeholder password through a secure deployment channel.
CREATE ROLE ai_usage_demo_reader LOGIN PASSWORD 'REPLACE_WITH_A_RANDOM_SECRET';

REVOKE ALL PRIVILEGES ON DATABASE ai_usage FROM ai_usage_demo_reader;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM ai_usage_demo_reader;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM ai_usage_demo_reader;
GRANT CONNECT ON DATABASE ai_usage TO ai_usage_demo_reader;
GRANT USAGE ON SCHEMA public TO ai_usage_demo_reader;

-- Snapshot reads used by the remote dashboard. Do not grant sequence, schema,
-- function, or broad database privileges.
GRANT SELECT ON TABLE
    usage_daily,
    usage_query_daily,
    usage_sync_coverage,
    usage_team_membership_daily,
    usage_department_directory,
    usage_snapshot_state,
    usage_sync_state
TO ai_usage_demo_reader;

ALTER ROLE ai_usage_demo_reader SET default_transaction_read_only = on;

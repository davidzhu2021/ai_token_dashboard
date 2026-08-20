\pset pager off
SELECT request_key, start_date, end_date, status, requested_at, claimed_at, completed_at, attempts, last_error
FROM usage_refresh_requests
WHERE request_key = 'c9edd3808e5755c8f96e43caeb2bd0dd4f172db6ce079f5996715a0776ae8fd0';

-- 最近 10 次同步运行
SELECT id, backend_id, start_date, end_date, row_count, status, started_at, finished_at
FROM usage_sync_runs
ORDER BY started_at DESC
LIMIT 10;

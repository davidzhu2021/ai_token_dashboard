-- 触发 30 天用量刷新（走 worker 刷新队列；幂等，重复执行无副作用）
INSERT INTO usage_refresh_requests (
    request_key, start_date, end_date, status, requested_at,
    claimed_at, completed_at, attempts, last_error
) VALUES (
    'c9edd3808e5755c8f96e43caeb2bd0dd4f172db6ce079f5996715a0776ae8fd0',
    '2026-07-20', '2026-08-18', 'pending', now(),
    NULL, NULL, 0, ''
) ON CONFLICT (request_key) DO NOTHING;

SELECT request_key, start_date, end_date, status, requested_at
FROM usage_refresh_requests
WHERE request_key = 'c9edd3808e5755c8f96e43caeb2bd0dd4f172db6ce079f5996715a0776ae8fd0';

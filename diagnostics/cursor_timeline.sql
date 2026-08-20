-- cursor-zhuyida / claude-code-zhuyida 用量与团队快照时间线（只读）
\pset pager off

-- 1) 未归因用量按日期分布（primary/cursor-zhuyida）
SELECT u.usage_date::text AS date, u.source,
       SUM(u.total_tokens) AS tokens, ROUND(SUM(u.spend)::numeric,4) AS spend,
       COUNT(*) AS groups
FROM usage_query_daily u
WHERE u.backend_id='primary' AND u.user_id='cursor-zhuyida'
  AND u.attribution_source='unattributed'
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
GROUP BY u.usage_date, u.source
ORDER BY u.usage_date;

-- 2) primary 后端 cursor-zhuyida 的团队快照时间线（近 45 天）
SELECT m.snapshot_date::text AS date, m.team_id, m.team_name, m.team_role
FROM usage_team_membership_daily m
WHERE m.backend_id='primary' AND m.user_id='cursor-zhuyida'
  AND m.snapshot_date >= CURRENT_DATE - INTERVAL '45 days'
ORDER BY m.snapshot_date, m.team_name;

-- 3) 未归因行里有 key 的：看 key 是否在客户令牌表里注册过
SELECT DISTINCT u.key_id
FROM usage_query_daily u
WHERE u.backend_id='primary' AND u.user_id='cursor-zhuyida'
  AND u.attribution_source='unattributed' AND u.key_id <> ''
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
LIMIT 20;

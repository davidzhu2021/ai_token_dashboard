-- zhuyida 无团队归属用量的归因构成（只读，近 30 天）
\pset pager off

-- 按 user_id / 来源 / 归因来源拆分
SELECT u.backend_id, u.user_id, u.source, u.attribution_source,
       COUNT(*) AS groups,
       SUM(u.total_tokens) AS tokens,
       ROUND(SUM(u.spend)::numeric,4) AS spend
FROM usage_query_daily u
WHERE lower(u.employee_email) = 'zhuyida@auto-link.com.cn'
  AND u.team_id = ''
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
GROUP BY u.backend_id, u.user_id, u.source, u.attribution_source
ORDER BY SUM(u.total_tokens) DESC;

-- 无团队部分里 key_id / organization_id 的覆盖情况
SELECT CASE WHEN u.key_id = '' THEN '(无key)' ELSE '有key' END AS has_key,
       CASE WHEN u.organization_id = '' THEN '(无org)' ELSE '有org' END AS has_org,
       COUNT(*) AS groups,
       SUM(u.total_tokens) AS tokens,
       ROUND(SUM(u.spend)::numeric,4) AS spend
FROM usage_query_daily u
WHERE lower(u.employee_email) = 'zhuyida@auto-link.com.cn'
  AND u.team_id = ''
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
GROUP BY 1, 2
ORDER BY SUM(u.total_tokens) DESC;

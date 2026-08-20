-- cursor-zhuyida 用量行按 employee_email 是否为空拆分（只读，近 30 天）
\pset pager off

SELECT CASE WHEN u.employee_email = '' THEN '(email空)' ELSE '(有email)' END AS has_email,
       u.source, u.attribution_source,
       COUNT(*) AS groups,
       SUM(u.total_tokens) AS tokens,
       ROUND(SUM(u.spend)::numeric,4) AS spend
FROM usage_query_daily u
WHERE u.backend_id='primary' AND u.user_id='cursor-zhuyida'
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
GROUP BY 1, 2, 3
ORDER BY SUM(u.total_tokens) DESC;

-- 无 email 且未归因的行按日期
SELECT u.usage_date::text AS date, u.source, COUNT(*) AS groups,
       SUM(u.total_tokens) AS tokens, ROUND(SUM(u.spend)::numeric,4) AS spend
FROM usage_query_daily u
WHERE u.backend_id='primary' AND u.user_id='cursor-zhuyida'
  AND u.employee_email = ''
  AND u.attribution_source='unattributed'
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
GROUP BY u.usage_date, u.source
ORDER BY u.usage_date;

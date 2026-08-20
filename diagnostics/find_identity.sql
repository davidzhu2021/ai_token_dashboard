-- 查找 zhuyida 在用量快照中的实际身份（只读）
SELECT employee_email, employee_name, COUNT(*) AS rows, SUM(total_tokens) AS tokens
FROM usage_query_daily
WHERE lower(employee_email) LIKE '%zhuyida%'
   OR lower(employee_name) LIKE '%zhuyida%'
   OR lower(user_id) LIKE '%zhuyida%'
   OR lower(employee_email) LIKE '%zhu%yida%'
GROUP BY employee_email, employee_name
ORDER BY COUNT(*) DESC
LIMIT 20;

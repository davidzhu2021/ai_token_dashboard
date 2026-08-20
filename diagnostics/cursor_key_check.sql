-- 检查 cursor 未归因行的 key 是否在客户令牌/导入 key 表里（只读）
\pset pager off

-- 1) 这两个 key 在 customer_access_token（托管令牌）里的记录
SELECT t.upstream_key_id, t.upstream_key_hash, t.upstream_key_alias,
       t.upstream_team_id, t.member_id, t.department_id,
       o.upstream_organization_id, m.upstream_user_id
FROM customer_access_token t
LEFT JOIN customer_organization o ON o.id=t.organization_id
LEFT JOIN customer_member m ON m.id=t.member_id
WHERE t.upstream_key_id IN ('28b66bde99907ecab73e2b287529b66a1eb033d9cef5af7b7f9d22346120d948',
                            'cf68275759ac3da52eff7b63dcd958a7fe29ae6e4e6fdf339fcab9eb51afb63d')
   OR t.upstream_key_hash IN ('28b66bde99907ecab73e2b287529b66a1eb033d9cef5af7b7f9d22346120d948',
                              'cf68275759ac3da52eff7b63dcd958a7fe29ae6e4e6fdf339fcab9eb51afb63d');

-- 2) 这两个 key 在 customer_usage_key_identity（report_only 导入）里的记录
SELECT k.backend_id, k.upstream_key_id, k.upstream_key_hash,
       k.upstream_team_id_snapshot, k.organization_id,
       k.mode, k.effective_from, k.effective_through
FROM customer_usage_key_identity k
WHERE k.upstream_key_id IN ('28b66bde99907ecab73e2b287529b66a1eb033d9cef5af7b7f9d22346120d948',
                            'cf68275759ac3da52eff7b63dcd958a7fe29ae6e4e6fdf339fcab9eb51afb63d')
   OR k.upstream_key_hash IN ('28b66bde99907ecab73e2b287529b66a1eb033d9cef5af7b7f9d22346120d948',
                              'cf68275759ac3da52eff7b63dcd958a7fe29ae6e4e6fdf339fcab9eb51afb63d');

-- 3) 该邮箱所有用量行里 team_id 为空的日期分布（确认是否近期才出现）
SELECT u.usage_date::text AS date, u.source, u.backend_id || ':' || u.user_id AS account,
       COUNT(*) AS groups, SUM(u.total_tokens) AS tokens
FROM usage_query_daily u
WHERE lower(u.employee_email) = 'zhuyida@auto-link.com.cn'
  AND u.team_id = ''
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '45 days' AND CURRENT_DATE
GROUP BY u.usage_date, u.source, u.backend_id, u.user_id
ORDER BY u.usage_date;

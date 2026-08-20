-- 精确复现 team_member_rows 对两个候选团队 scope 的结果（只读）
-- scope 团队：AI Infra部(team-2ed30984b00bb482, backends her+primary) 与 AI技术院(team-89a8a9e42a3b64fe, primary)
\pset pager off

-- A) AI Infra部 scope：zhuyida 在快照中匹配到的成员（真实 team_member_rows selected 逻辑）
WITH scope(backend_id, team_id) AS (
    SELECT * FROM unnest(ARRAY['her','primary']::text[], ARRAY['team-2ed30984b00bb482','team-2ed30984b00bb482']::text[])
),
selected AS (
    SELECT DISTINCT ON (m.backend_id, m.user_id) m.backend_id, m.user_id, m.employee_email, m.team_id, m.snapshot_date
    FROM usage_team_membership_daily m JOIN scope s ON s.backend_id=m.backend_id AND s.team_id=m.team_id
    WHERE m.snapshot_date <= CURRENT_DATE
      AND (lower(btrim(m.user_id)) = 'zhuyida@auto-link.com.cn'
           OR lower(btrim(m.employee_email)) = 'zhuyida@auto-link.com.cn'
           OR lower(btrim(m.employee_name)) = 'zhuyida@auto-link.com.cn')
    ORDER BY m.backend_id, m.user_id, m.snapshot_date DESC
)
SELECT 'A-selected' AS part, s.backend_id, s.user_id, s.employee_email, s.team_id, s.snapshot_date::text FROM selected s;

-- B) AI Infra部 scope：真实 team_member_rows 用量匹配（近 30 天）
WITH scope(backend_id, team_id) AS (
    SELECT * FROM unnest(ARRAY['her','primary']::text[], ARRAY['team-2ed30984b00bb482','team-2ed30984b00bb482']::text[])
),
selected AS (
    SELECT DISTINCT ON (m.backend_id, m.user_id) m.backend_id, m.user_id, m.employee_email
    FROM usage_team_membership_daily m JOIN scope s ON s.backend_id=m.backend_id AND s.team_id=m.team_id
    WHERE m.snapshot_date <= CURRENT_DATE
      AND (lower(btrim(m.user_id)) = 'zhuyida@auto-link.com.cn'
           OR lower(btrim(m.employee_email)) = 'zhuyida@auto-link.com.cn'
           OR lower(btrim(m.employee_name)) = 'zhuyida@auto-link.com.cn')
    ORDER BY m.backend_id, m.user_id, m.snapshot_date DESC
)
SELECT 'B-usage' AS part, SUM(u.total_tokens) AS tokens, ROUND(SUM(u.spend)::numeric,4) AS spend, COUNT(*) AS groups,
       array_agg(DISTINCT u.backend_id || ':' || u.user_id) AS matched_users
FROM usage_query_daily u
WHERE u.backend_id=ANY(ARRAY['her','primary'])
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
  AND EXISTS (SELECT 1 FROM selected s WHERE s.backend_id=u.backend_id AND (s.user_id=u.user_id OR (NULLIF(btrim(s.employee_email),'') IS NOT NULL AND lower(btrim(s.employee_email))=lower(btrim(u.employee_email)))))
  AND EXISTS (SELECT 1 FROM scope sc WHERE sc.backend_id=u.backend_id AND sc.team_id=u.team_id);

-- C) AI技术院 scope：zhuyida 匹配到的成员
WITH scope(backend_id, team_id) AS (
    SELECT * FROM unnest(ARRAY['primary']::text[], ARRAY['team-89a8a9e42a3b64fe']::text[])
),
selected AS (
    SELECT DISTINCT ON (m.backend_id, m.user_id) m.backend_id, m.user_id, m.employee_email, m.team_id, m.snapshot_date
    FROM usage_team_membership_daily m JOIN scope s ON s.backend_id=m.backend_id AND s.team_id=m.team_id
    WHERE m.snapshot_date <= CURRENT_DATE
      AND (lower(btrim(m.user_id)) = 'zhuyida@auto-link.com.cn'
           OR lower(btrim(m.employee_email)) = 'zhuyida@auto-link.com.cn'
           OR lower(btrim(m.employee_name)) = 'zhuyida@auto-link.com.cn')
    ORDER BY m.backend_id, m.user_id, m.snapshot_date DESC
)
SELECT 'C-selected' AS part, s.backend_id, s.user_id, s.employee_email, s.team_id, s.snapshot_date::text FROM selected s;

-- D) AI技术院 scope：真实 team_member_rows 用量匹配（近 30 天）
WITH scope(backend_id, team_id) AS (
    SELECT * FROM unnest(ARRAY['primary']::text[], ARRAY['team-89a8a9e42a3b64fe']::text[])
),
selected AS (
    SELECT DISTINCT ON (m.backend_id, m.user_id) m.backend_id, m.user_id, m.employee_email
    FROM usage_team_membership_daily m JOIN scope s ON s.backend_id=m.backend_id AND s.team_id=m.team_id
    WHERE m.snapshot_date <= CURRENT_DATE
      AND (lower(btrim(m.user_id)) = 'zhuyida@auto-link.com.cn'
           OR lower(btrim(m.employee_email)) = 'zhuyida@auto-link.com.cn'
           OR lower(btrim(m.employee_name)) = 'zhuyida@auto-link.com.cn')
    ORDER BY m.backend_id, m.user_id, m.snapshot_date DESC
)
SELECT 'D-usage' AS part, SUM(u.total_tokens) AS tokens, ROUND(SUM(u.spend)::numeric,4) AS spend, COUNT(*) AS groups,
       array_agg(DISTINCT u.backend_id || ':' || u.user_id) AS matched_users
FROM usage_query_daily u
WHERE u.backend_id=ANY(ARRAY['primary'])
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
  AND EXISTS (SELECT 1 FROM selected s WHERE s.backend_id=u.backend_id AND (s.user_id=u.user_id OR (NULLIF(btrim(s.employee_email),'') IS NOT NULL AND lower(btrim(s.employee_email))=lower(btrim(u.employee_email)))))
  AND EXISTS (SELECT 1 FROM scope sc WHERE sc.backend_id=u.backend_id AND sc.team_id=u.team_id);

-- E) 若前端传 employee=user_id（如 claude-code-zhuyida），AI Infra部 scope 用量匹配
WITH scope(backend_id, team_id) AS (
    SELECT * FROM unnest(ARRAY['her','primary']::text[], ARRAY['team-2ed30984b00bb482','team-2ed30984b00bb482']::text[])
),
selected AS (
    SELECT DISTINCT ON (m.backend_id, m.user_id) m.backend_id, m.user_id, m.employee_email
    FROM usage_team_membership_daily m JOIN scope s ON s.backend_id=m.backend_id AND s.team_id=m.team_id
    WHERE m.snapshot_date <= CURRENT_DATE
      AND (lower(btrim(m.user_id)) IN ('claude-code-zhuyida','cursor-zhuyida','carher-271')
           OR lower(btrim(m.employee_email)) IN ('claude-code-zhuyida','cursor-zhuyida','carher-271')
           OR lower(btrim(m.employee_name)) IN ('claude-code-zhuyida','cursor-zhuyida','carher-271'))
    ORDER BY m.backend_id, m.user_id, m.snapshot_date DESC
)
SELECT 'E-usage' AS part, SUM(u.total_tokens) AS tokens, ROUND(SUM(u.spend)::numeric,4) AS spend, COUNT(*) AS groups,
       array_agg(DISTINCT u.backend_id || ':' || u.user_id) AS matched_users
FROM usage_query_daily u
WHERE u.backend_id=ANY(ARRAY['her','primary'])
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
  AND EXISTS (SELECT 1 FROM selected s WHERE s.backend_id=u.backend_id AND (s.user_id=u.user_id OR (NULLIF(btrim(s.employee_email),'') IS NOT NULL AND lower(btrim(s.employee_email))=lower(btrim(u.employee_email)))))
  AND EXISTS (SELECT 1 FROM scope sc WHERE sc.backend_id=u.backend_id AND sc.team_id=u.team_id);

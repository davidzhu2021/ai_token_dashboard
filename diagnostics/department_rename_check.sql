-- 部门目录中 AI Infra部 / AI技术院 的状态与成员数（只读）
\pset pager off

SELECT dd.backend_id, dd.department_id, dd.department_name,
       dd.organization_id, dd.status, dd.synced_at::text
FROM usage_department_directory dd
WHERE dd.department_id IN ('team-2ed30984b00bb482','team-89a8a9e42a3b64fe')
ORDER BY dd.backend_id, dd.department_name;

-- 这两个团队在成员快照中的最新成员数与角色
SELECT m.backend_id, m.team_id, m.team_name,
       COUNT(DISTINCT m.user_id) AS members,
       COUNT(*) FILTER (WHERE lower(m.team_role)='admin') AS admins,
       MAX(m.snapshot_date)::text AS latest_snapshot
FROM usage_team_membership_daily m
WHERE m.team_id IN ('team-2ed30984b00bb482','team-89a8a9e42a3b64fe')
  AND m.snapshot_date >= CURRENT_DATE - 1
GROUP BY m.backend_id, m.team_id, m.team_name
ORDER BY m.team_name;

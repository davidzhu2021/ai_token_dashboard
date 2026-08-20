-- ============================================================================
-- 用量看板口径对账（只读诊断脚本）
--
-- 背景：负责人发现「我的用量」与「团队看板 / 部门看板」下钻自身的数据不一致，
--       而「全员看板」下钻一致。代码分析表明各看板对用量行 team_id 的过滤
--       口径不同：个人/全员看板不要求团队归属，团队/部门看板强依赖 team_id。
--       本脚本用于在只读环境下确认差距的实际构成（无团队 / 跨团队 / 快照缺失）。
--
-- 用法（在 188 服务器上）：
--   1) 把下面 \set email 后面的邮箱改成实际登录邮箱（默认取 git 提交者邮箱）
--   2) 执行：
--        cd /home/cltx/apps/ai-token-dashboard/current
--        docker compose exec -T usage-db psql -U ai_dashboard -d ai_usage -f usage_board_reconcile.sql
--   3) 把完整输出发给开发/负责人核对
--
-- 注意：全部为 SELECT 只读查询，不会修改任何数据。
-- ============================================================================

\set email 'zhuyida@auto-link.com.cn'
\pset pager off

-- ----------------------------------------------------------------------------
-- 0) 确认邮箱在用量快照中存在（若结果为空，说明该邮箱不对，先改 \set email）
-- ----------------------------------------------------------------------------
SELECT COUNT(*) AS usage_records,
       COUNT(DISTINCT user_id) AS user_ids,
       COUNT(DISTINCT backend_id) AS backends
FROM usage_query_daily
WHERE lower(employee_email) = lower(:'email');

-- ----------------------------------------------------------------------------
-- 1) 该邮箱近 30 天用量按 team_id 拆分 —— 看「无团队(空串)」占比
--    空串行 = 「我的用量 / 全员下钻」可见、但「团队 / 部门看板」不可见的行
-- ----------------------------------------------------------------------------
SELECT COALESCE(NULLIF(u.team_id, ''), '(空/未归属团队)') AS team_id,
       COUNT(*) AS record_groups,
       SUM(u.total_tokens) AS total_tokens,
       ROUND(SUM(u.spend)::numeric, 4) AS spend,
       array_agg(DISTINCT u.user_id) AS user_ids,
       array_agg(DISTINCT u.backend_id) AS backends
FROM usage_query_daily u
WHERE lower(u.employee_email) = lower(:'email')
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
GROUP BY u.team_id
ORDER BY SUM(u.total_tokens) DESC NULLS LAST;

-- ----------------------------------------------------------------------------
-- 2) 该邮箱在团队成员快照(usage_team_membership_daily)中的记录
--    看：快照里有哪些团队、user_id 是否与用量一致、最新快照日期是否滞后
-- ----------------------------------------------------------------------------
SELECT m.backend_id, m.team_id, m.team_name, m.user_id,
       m.employee_email, m.employee_name, m.team_role,
       MAX(m.snapshot_date) AS latest_snapshot_date,
       COUNT(DISTINCT m.snapshot_date) AS snapshot_days
FROM usage_team_membership_daily m
WHERE lower(m.employee_email) = lower(:'email')
   OR m.user_id IN (
       SELECT DISTINCT user_id FROM usage_query_daily
       WHERE lower(employee_email) = lower(:'email')
   )
GROUP BY m.backend_id, m.team_id, m.team_name, m.user_id,
         m.employee_email, m.employee_name, m.team_role
ORDER BY m.team_name, m.backend_id, m.user_id;

-- ----------------------------------------------------------------------------
-- 3) 四个看板口径近 30 天总量对比（同一批数据，不同过滤条件）
--    预期：个人 = 全员 > 部门 >= 团队；差多少就是口径差异的量
-- ----------------------------------------------------------------------------
WITH personal AS (
    SELECT SUM(total_tokens) AS tokens, ROUND(SUM(spend)::numeric, 4) AS spend
    FROM usage_query_daily
    WHERE lower(employee_email) = lower(:'email')
      AND usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
),
admin_drill AS (
    SELECT SUM(total_tokens) AS tokens, ROUND(SUM(spend)::numeric, 4) AS spend
    FROM usage_query_daily
    WHERE (position(lower(:'email') IN lower(user_id)) > 0
        OR position(lower(:'email') IN lower(employee_email)) > 0
        OR position(lower(:'email') IN lower(employee_name)) > 0)
      AND usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
),
department_drill AS (
    SELECT SUM(total_tokens) AS tokens, ROUND(SUM(spend)::numeric, 4) AS spend
    FROM usage_query_daily
    WHERE lower(employee_email) = lower(:'email')
      AND team_id <> ''
      AND usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
),
team_drill AS (
    SELECT SUM(u.total_tokens) AS tokens, ROUND(SUM(u.spend)::numeric, 4) AS spend
    FROM usage_query_daily u
    JOIN (
        SELECT DISTINCT ON (backend_id, user_id) backend_id, team_id, user_id
        FROM usage_team_membership_daily
        WHERE lower(employee_email) = lower(:'email')
        ORDER BY backend_id, user_id, snapshot_date DESC
    ) m ON m.backend_id = u.backend_id AND m.user_id = u.user_id
       AND u.team_id = m.team_id
    WHERE u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
)
SELECT '1 我的用量(按邮箱)' AS view_name, tokens, spend FROM personal
UNION ALL SELECT '2 全员下钻(子串匹配)', tokens, spend FROM admin_drill
UNION ALL SELECT '3 部门下钻(仅team非空)', tokens, spend FROM department_drill
UNION ALL SELECT '4 团队下钻(快照+team匹配)', tokens, spend FROM team_drill;

-- ----------------------------------------------------------------------------
-- 4) 该邮箱所在团队的快照覆盖情况 —— 看快照是否滞后于最新日期
--    days_behind 越大，团队看板越可能显示旧成员名单
-- ----------------------------------------------------------------------------
SELECT m.backend_id, m.team_id, m.team_name,
       MAX(m.snapshot_date) AS latest_snapshot_date,
       (CURRENT_DATE - MAX(m.snapshot_date)) AS days_behind,
       COUNT(DISTINCT m.user_id) AS members_in_snapshot
FROM usage_team_membership_daily m
WHERE m.team_id IN (
    SELECT DISTINCT team_id FROM usage_team_membership_daily
    WHERE lower(employee_email) = lower(:'email')
)
GROUP BY m.backend_id, m.team_id, m.team_name
ORDER BY m.team_name, m.backend_id;

-- ----------------------------------------------------------------------------
-- 5) 差距构成明细：该邮箱近 30 天按 source × team_id 拆分
--    用于判断「无团队」的用量主要来自哪些来源（Codex / Claude Code / 其他）
-- ----------------------------------------------------------------------------
SELECT u.source,
       COALESCE(NULLIF(u.team_id, ''), '(空/未归属团队)') AS team_id,
       SUM(u.total_tokens) AS total_tokens,
       ROUND(SUM(u.spend)::numeric, 4) AS spend
FROM usage_query_daily u
WHERE lower(u.employee_email) = lower(:'email')
  AND u.usage_date BETWEEN CURRENT_DATE - INTERVAL '30 days' AND CURRENT_DATE
GROUP BY u.source, u.team_id
ORDER BY u.source, u.team_id;

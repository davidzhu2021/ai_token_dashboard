# CLAUDE.md

本项目的全部协作约束定义在 AGENTS.md 中，Claude Code 必须完整遵守：

@AGENTS.md

## 当前生产迁移边界

- 正式看板运行在新服务器 `AIYJY-243 / 10.68.13.243` 的单节点 K3s，命名空间为 `ai-token-dashboard`。
- PostgreSQL、Redis、认证/密钥库、邮件中继数据和迁移临时文件使用新机数据盘 `/Data`（PVC 实际路径为 `/Data/k3s-storage`）。
- 正式域名 `myai.carher.net` 通过独立 Cloudflare Tunnel `ai-token-dashboard-myai` 进入新机；旧机共享 Tunnel `carher-s3` 仍承载其他业务，不得停用。
- 后续应用写入、实时同步和历史回填只能由新机 K3s 工作负载完成。禁止在旧服务器重新启动 Docker 看板或 worker，避免双写和数据分叉。
- 旧服务器只保留回滚用的 Compose volume、数据库备份、认证/密钥库和必要 Tunnel 配置；迁移稳定前不得删除。
- 变更生产配置或代码时，先在仓库提交并推送，再按 `AGENTS.md` 的生产同步流程操作；不得把 `.env`、Secret、Tunnel 凭据或其他密钥写入 Git、日志或文档。

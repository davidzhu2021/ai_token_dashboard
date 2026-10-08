# 迁移运行手册

以下命令需要在维护窗口执行。命令中的密码、密钥、Tunnel 凭据和生产
`.env` 必须通过受控通道提供，不能写入 Git 或终端输出。

## 预检

```bash
kubectl apply --dry-run=client -k deploy/k8s
kubectl get node,pod -A
df -h / /Data
```

Create secrets separately from the protected production environment file:

```bash
kubectl -n ai-token-dashboard create secret generic dashboard-secrets \
  --from-env-file=/secure/ai-token-dashboard/.env --dry-run=client -o yaml | kubectl apply -f -
kubectl -n ai-token-dashboard create secret generic cloudflared-credentials \
  --from-file=credentials.json=/secure/cloudflared/<tunnel-uuid>.json \
  --dry-run=client -o yaml | kubectl apply -f -
```

Replace the tunnel UUID in `cloudflared.yaml` and verify the credential file
belongs to that tunnel before applying it separately.

确认新机 K3s 节点为 `Ready`，`/Data` 剩余空间大于 30%，并且旧 LiteLLM
Pod 状态没有变化。

## 镜像和数据

从旧机导出 `ai-token-dashboard:latest`，在新机导入：

```bash
docker save ai-token-dashboard:latest | ssh cltx@10.68.13.243 \
  'sudo k3s ctr images import -'
```

在线阶段导出 PostgreSQL 到 `/Data/migration/`，传输后恢复到新 PVC。认证
SQLite、`key-vault-data`、DKIM 和 postfix spool 也复制到对应 PVC。恢复后
核对表数量、关键用户数量、密钥库文件和镜像 digest。

## 预部署验证

```bash
kubectl apply -k deploy/k8s
kubectl -n ai-token-dashboard get pvc,pod,svc,ingress
kubectl -n ai-token-dashboard rollout status statefulset/usage-db
kubectl -n ai-token-dashboard rollout status deployment/ai-token-dashboard
kubectl -n ai-token-dashboard port-forward svc/ai-token-dashboard 18000:8000
curl -fsS http://127.0.0.1:18000/api/health
```

During restore, keep both worker Deployments scaled to zero. Scale them back to
one only after PostgreSQL and the persistent application data have been
verified.

先验证临时入口、登录、普通用户和管理员看板、模型列表、密钥页面以及
两个 worker 日志；不要在旧机仍提供写入口时启动新机正式 Tunnel。

## 最终切换与回滚

维护窗口开始后停止旧 Compose 和旧 Tunnel，执行最终 PostgreSQL dump、
Redis 快照和 `.data`/邮件目录同步，恢复新 PVC 并重启相关 Deployment。
确认新机健康检查通过后再启用新 Tunnel。失败时删除新机 Tunnel/Ingress
入口，恢复旧 Compose 和旧 Tunnel；迁移当天不删除旧机 volume 或备份。

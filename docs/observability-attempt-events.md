# 稳定性看板 attempt 事件采集链路（P0）

## 背景

稳定性看板的「**兜底成功率**」需要*尝试级*事件（每次上游调用：原始尝试、retry、fallback），
而上游 `/spend/logs/v2` 原始日志只有每条请求的**最终状态**，无法还原 fallback 过程。
spend log 的 metadata 只携带 `attempted_retries / max_retries / status / error_information`，
**没有 fallback 信息**（fallback 计数只出现在成功响应的 `x-litellm-attempted-fallbacks` 头）。

因此 P0 的目标是把 attempt 事件从 LiteLLM Proxy 侧**推送**到看板采集接口：

```
LiteLLM Proxy (custom callback)
   └─ 每次 attempt 成功/失败 → 批量 POST
        └─ /api/internal/observability/events（HMAC 签名）
             └─ stability_attempt_events 表
                  └─ 稳定性看板「兜底成功率 / 上游异常率 / 重试恢复率」
```

配套增强：spend-log 同步时（`UsageStore.publish_stability_events`）会把每条最终请求
补写为 `event_type='final_request'` 的尝试事件，让「上游异常率」「重试恢复率」在
推送方接入**之前**即可从原始日志推导；「兜底恢复率」仍只依赖推送方。

## 一、看板侧配置（本仓库，JSZX-AI-03 / myai.carher.net）

1. 在服务器 `.env` 生成独立随机密钥（不要与其他密钥复用）：

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. 写入 `.env` 并重启服务：

   ```dotenv
   OBSERVABILITY_INGEST_HMAC_SECRET=<上一步生成的密钥>
   ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED=true
   ```

3. 验证采集接口已启用（401 = 已启用但签名无效；404 = 未配置密钥）：

   ```bash
   curl -i -X POST https://myai.carher.net/api/internal/observability/events \
     -H 'Content-Type: application/json' -H 'x-observability-timestamp: 0' \
     -H 'x-observability-signature: sha256=0' -d '{"events":[]}'
   ```

## 二、自检链路（可选，先于推送方部署）

在本地（或能访问看板的主机）运行自检脚本，验证签名算法与入库：

```bash
python scripts/observability_ingest_check.py \
  --secret <OBSERVABILITY_INGEST_HMAC_SECRET> \
  --backend-id <backend_id> \
  --dry-run                # 先看请求；确认无误后去掉 --dry-run 真正发送
```

发送成功后在稳定性看板「场景样本」中可见 `status=test` 的样本。
`backend_id` 必须与看板 `usage_backend_ids()` 返回的 id 一致
（remote-demo 只读模式对应 `USAGE_SNAPSHOT_BACKEND_IDS`）。

## 三、LiteLLM 侧推送方（K3s `litellm-product` 集群）

交付文件：`integrations/litellm/observability_attempt_pusher.py`（`CustomLogger` 子类）。

1. 把该文件放到 LiteLLM Proxy 能导入的位置。生产已有自定义 callbacks 挂载机制
   （`litellm-callbacks` 等 configmap），沿用同样的挂载方式；或放在 config.yaml
   同目录（LiteLLM `get_instance_fn` 会按 config 文件目录加载 `<module>.py`）。

2. 在 config.yaml（或 DB 配置）注册 callback：

   ```yaml
   litellm_settings:
     callbacks:
       - observability_attempt_pusher.attempt_pusher_instance
   ```

3. 配置环境变量（与看板 `.env` 对应，密钥相同）：

   ```dotenv
   OBSERVABILITY_INGEST_URL=https://myai.carher.net/api/internal/observability/events
   OBSERVABILITY_INGEST_HMAC_SECRET=<与看板相同>
   OBSERVABILITY_BACKEND_ID=<backend_id，与看板 usage_backend_ids() 一致>
   ```

   可选调优：`OBSERVABILITY_INGEST_BATCH_MAX`（默认 100）、
   `OBSERVABILITY_INGEST_FLUSH_SECONDS`（默认 3）、
   `OBSERVABILITY_INGEST_TIMEOUT_SECONDS`（默认 3）。

4. 滚动重启 `litellm-proxy`，观察日志出现
   `observability attempt pusher enabled backend=...`。

行为保证：
- 每次 attempt（成功/失败）异步入队批量推送，**绝不阻塞代理主流程**；
- 事件字段严格限定在看板白名单内，错误消息发送前脱敏截断，不含 prompt/response/key；
- `eventId = backend_id:call_id:attempt_index` 幂等，重推不会重复；
- 任一配置缺失时推送方自动禁用，仅记录日志。

## 四、验收

1. `stability_attempt_events` 表出现 `event_type='final_request'` 之外的事件
   （`success` / `failure`，来自推送方）；
2. 稳定性看板顶部「兜底成功率」卡片不再是「需接入显式兜底尝试事件」，
   显示真实比例；「上游异常率」「重试恢复率」在 spend-log 同步后即可见；
3. 请求抽屉的「尝试时间线」能展示 retry / fallback 过程。

## 五、口径说明

- 「兜底成功率」分母 = 经历过 fallback 的请求（成功响应带
  `x-litellm-attempted-fallbacks` 头，或推送方标记 `isFallback`），
  分子 = 其中最终成功的请求；
- spend-log 推导的 `final_request` 事件 `is_fallback=False`，**不会**混入兜底统计；
- 重试计数直接使用 Router 写入 metadata 的 `attempted_retries`（首次尝试为 0）。

# 模型广场改版：厂商图标 + 后端价格

仿照参照站点重做模型广场：每张卡带厂商彩色图标、标出计费标准与真实单价，并新增表格视图便于横向比价。

## 已确认的数据事实（实测，非推测）

对照 `D:\litellm` 源码与上游实测：

- 价格字段来自上游 `/model/info` 的 `model_info`：`input_cost_per_token`、`output_cost_per_token`、`cache_read_input_token_cost`、`cache_creation_input_token_cost`，另有 `mode`（`chat` / `responses` / `embedding` / `image_generation`）、`max_input_tokens`、`supports_vision` / `supports_reasoning` / `supports_function_calling`。
- 当前目录 135 条模型全部能在 `/model/info` 匹配到记录，无遗漏。
- **同一模型名在不同部署/后端上单价不一致**。LiteLLM 官方 `_set_model_group_info`（`litellm/router.py:8776-8780`）的口径是取同组各部署单价的**最大值**，本方案沿用该口径。
- 主网关中 `gpt-5.2`、`gpt-5.4-mini`、`codex-auto-review`、`claude-kimi-k3` 是透传部署，任何部署上都查不到非零单价。
- 单价为「每 token」，展示需 ×1,000,000 换算为 `/ 1M Tokens`。

## 决策（已与用户确认）

1. **价格口径**：直接展示上游真实单价，不做倍率换算。
2. **清单范围**：只展示干净主名，过滤内部网关部署别名（`wangsu-` / `zerokey-` / `zai-` / `local-` / `openrouter-` / `kuaihui-` / `chatgpt-` / `pool` / `liuguoxian` / `cheliantianxia1` / `max` 词元，以及含 `/` 或 `供应商.` 前缀的名字）。符合 AGENTS.md 前端产品边界。
3. **零价处理**：同一模型名下取有价格的那个部署；四个查不到任何非零价的模型**整条不展示**。实测结果：135 → 31 主名 → 展示 27 条。
4. **视图**：卡片（默认）+ 表格双视图切换。
5. **图标**：内置手写 SVG `<symbol>`，无外部依赖。

## 后端改动 `backend/litellm_client.py`

### 1. 新增厂商归类 `model_family(model_name) -> (key, label)`

现有 `provider_from_model` 只覆盖 5 家且把 GLM/Kimi/MiniMax 全归入「其他」。新增按模型名词根匹配的映射（不改动 `provider_from_model`，它仍服务于现有 `provider` 字段以免破坏既有筛选）：

| 词根 | key | 展示名 |
|---|---|---|
| `claude` | `anthropic` | Anthropic |
| `gpt` / `codex` / `image` | `openai` | OpenAI |
| `gemini` | `google` | Google |
| `deepseek` | `deepseek` | DeepSeek |
| `glm` | `zhipu` | 智谱 GLM |
| `kimi` | `moonshot` | 月之暗面 |
| `qwen` | `qwen` | 通义千问 |
| `minimax` | `minimax` | MiniMax |
| `bge` | `baai` | BAAI |
| 其他 | `other` | 其他 |

匹配顺序很重要：`claude-code-glm-5.1` 必须归 GLM 而非 Anthropic，所以先匹配具体厂商词根（glm/kimi/deepseek/qwen/minimax/gemini/bge），最后才落 gpt/codex/claude。

### 2. 新增 `_load_model_pricing()`：拉取并按名聚合价格

对每个 backend 请求 `GET /model/info`，按 `model_name` casefold 聚合，同名多部署取 `input_cost_per_token` 最大的那条；跨 backend 再按同规则取最大。产出 `{normalized_name: {inputCost, outputCost, cacheReadCost, cacheWriteCost, mode, maxInputTokens, supportsVision, supportsReasoning, supportsFunctionCalling}}`。

- 复用现有 `self._model_cache`（TTL `MODEL_CACHE_TTL_SECONDS`，默认 1800s），缓存键 `model_pricing`。
- 单 backend 失败不影响其他 backend（沿用现有 `except HTTPException: continue` 模式）。
- 全部失败时返回空 dict，此时 `models()` 退化为不带价格的旧行为，不抛错。

### 3. 新增 `_is_internal_alias(model_name) -> bool`

按上面决策 2 的规则判定。注意 `gpt-5.2` 的 `.` 不能误判为供应商前缀 —— 用 `^[a-z][a-z0-9]*\.` 匹配且要求点后是字母（已实测 `gpt-5.2` 不被误伤）。

### 4. 改造 `models()`

在现有目录构建后追加：
- 用 `_is_internal_alias` 过滤别名条目；
- 用价格表补充新字段：`familyKey`、`familyLabel`、`billingType`（`按量计费` / `按次计费`，依 `mode == "image_generation"` 判定）、`inputPricePerMillion`、`outputPricePerMillion`、`cacheReadPricePerMillion`、`cacheWritePricePerMillion`、`contextWindow`（优先用价格表的 `max_input_tokens`）、`capabilities`（由 `supports_*` 生成：视觉 / 推理 / 函数调用 / 向量化）；
- **丢弃无非零价格的条目**（决策 3）；
- 保留现有按用量排序逻辑与 `modelName` / `provider` / `id` 字段，既有测试与密钥模型选择不受影响。

排序键沿用现有 `_normalized_model_name`（**不套 `normalize_model_display_name`** —— 记忆里明确记录目录侧刻意保留原始别名以匹配用量 breakdown key）。

价格 ×1e6 后按 4 位小数输出为 number，前端负责格式化。

## 前端改动

### `index.html`

1. 新增 10 个厂商 `<symbol>`（`icon-vendor-openai`、`-anthropic`、`-google`、`-deepseek`、`-zhipu`、`-moonshot`、`-qwen`、`-minimax`、`-baai`、`-other`）。这些是**填充式彩色**图标，与现有描边式 `.app-icon` 体系不同，需独立 CSS 类 `.vendor-icon`（`fill: currentColor; stroke: none`），每厂商一个品牌色 modifier 类。
2. 模型广场区块重构：
   - hero 保留，文案改为说明价格口径；
   - 筛选区从「搜索 + 供应商 + 能力」扩展为「搜索 + 厂商 + 计费类型 + 视图切换按钮组」；
   - 新增 `#modelTableWrap` 表格容器（列：模型 / 厂商 / 计费类型 / 输入价 / 补全价 / 缓存读取 / 上下文 / 操作），沿用现有 `.table-wrap` + `<table>` 样式；
   - `#modelGrid` 保留但卡片内部结构重做。
3. CSS：改造 `.model-card`（厂商图标 + 模型名 + 复制按钮头部、价格三行区、计费类型 chip）、新增 `.model-price-row`、`.vendor-icon`、`.model-view-toggle`，并更新 1600px / 820px 断点下的响应式规则（表格在窄屏走横向滚动）。
4. **必须同步更新末尾 `<script src="/assets/app.js?v=...">` 版本号**（记忆中的既有坑），改为 `v=20260728-model-plaza-pricing`。

### `assets/app.js`

- 新增 `modelViewMode`（`"card"` / `"table"`）状态与 `formatPricePerMillion(value)`（`$X.XXXX / 1M Tokens`，无价时返回 `-`）。
- `setupModelFilters()` 改为按 `familyLabel` 与 `billingType` 建选项，并在每个选项后带计数（参照站点的 `全部供应商 47` 效果）。
- `filteredModels()` 关键词匹配扩展到 `familyLabel`。
- `renderModels()` 依 `modelViewMode` 分派到 `renderModelCards()` / `renderModelTable()`。
- **所有新增渲染字段一律走 `escapeHtml`**（记忆中记录现有模型卡片渲染未转义，新代码不沿用该缺陷）。
- 视图切换按钮事件绑定；表格行的复制按钮复用现有 `[data-copy-model]` 委托（把监听器从 `#modelGrid` 提到共同父容器，或给表格容器加一份同样的委托）。

## 测试 `tests/test_model_catalog_pricing.py`（新增）

沿用 `tests/test_model_usage_sort.py` 的 `make_client()` + `fake_request_backend` 打桩风格，覆盖：

1. 同名多部署取最大非零单价；
2. 跨 backend 价格冲突时取最大值；
3. 无任何非零价格的模型被丢弃；
4. 内部别名（`wangsu-gpt-5.5`、`zerokey-codex`、`anthropic.claude-opus-4-8`）被过滤，而 `gpt-5.2`、`claude-code-glm-5.1` 不被误伤；
5. `model_family` 归类正确，尤其 `claude-code-glm-5.1` → 智谱 GLM；
6. `/model/info` 全部失败时目录仍返回（不带价格，不抛错）；
7. 单价换算：`2.5e-06` → `2.5` per 1M。

同时跑现有 `tests/test_model_usage_sort.py` 与 `tests/test_her_multi_backend.py` 确认无回归 —— 注意这些测试的 `fake_request_backend` 会对未知 path 抛 `AssertionError`，新增的 `/model/info` 调用必须容错，否则会打挂既有测试。这点是本次改动最主要的回归风险，实现时优先处理。

## 验证

1. `python -m pytest tests/ -q`（全量，确认无回归）。
2. `127.0.0.1:8000` 起本地服务（遵守 AGENTS.md：仅用 8000，端口占用先查进程，收尾停掉临时服务），验证 `GET /api/health`、`GET /api/models` 返回带价格字段、模型广场卡片/表格双视图、筛选与搜索、复制按钮。
3. `git diff` review 后提交推送，再按 AGENTS.md 同步生产 JSZX-AI-03 并做健康检查。

## 范围外

不改动用量统计、密钥管理、`provider_from_model` 现有语义，不引入倍率/充值价配置，不动 landing 页。

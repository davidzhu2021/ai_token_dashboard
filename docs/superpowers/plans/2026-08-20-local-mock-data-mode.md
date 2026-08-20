# 本地 Mock 数据模式实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让回环地址本地开发默认完全使用进程内 mock 数据，只有显式设置 `LOCAL_DATA_MODE=real` 才访问真实上游；远端部署始终使用真实数据并拒绝 mock 模式。

**Architecture:** 在 `backend/mock_runtime.py` 提供单一运行模式判定、内存用量/模型/令牌/账单替身及固定种子重置能力；`backend/main.py` 在应用生命周期和数据依赖入口统一选择 mock 或真实实现。现有 `InMemoryOrganizationStore` 继续作为企业组织 mock 的基础，`mock_dev_server.py` 改为兼容启动入口而不再承担核心替换逻辑。

**Tech Stack:** FastAPI、Python 3.12、pytest、现有 `AuthStore`/`InMemoryOrganizationStore`/`UsageStore` 接口、浏览器端现有 API 合约。

**Spec:** 本次对话中批准的“本地默认 mock、显式 real、远端真实；mock 进程内可变、重启复位；所有业务接口覆盖”设计。

## Global Constraints

- 远端 HTTPS 部署不得启用开发登录或 mock 数据；任何误配置必须 fail closed。
- mock 不得发出 LiteLLM、数据库、OIDC、SMTP、支付网关或其他外部请求。
- 前端不得新增 LiteLLM 或上游实现细节文案。
- 本地验证只使用 `http://127.0.0.1:8000`；不静默切换端口。
- 保留用户现有未跟踪文件和并发修改，不提交 `.env`、密钥、日志、虚拟环境或生成物。

### Task 1: 运行模式判定与安全边界

**Files:**
- Create: `backend/mock_runtime.py`
- Modify: `backend/main.py:138-174, 609-639, 779-842, 1269-1360, 1682-1740`
- Modify: `.env.example`
- Test: `tests/test_local_data_mode.py`

**Interfaces:**
- Produce `local_data_mode() -> Literal["mock", "real"]`.
- Produce `local_mock_enabled() -> bool` and `validate_local_data_mode() -> None`.
- Mock is selected only for HTTP loopback `APP_BASE_URL`, defaults to mock there, accepts `LOCAL_DATA_MODE=real`, and raises a startup `RuntimeError` for non-loopback `LOCAL_DATA_MODE=mock`.
- `app_lifespan` validates the mode before starting billing, organization, or usage workers; mock mode skips all external worker/store initialization.

- [ ] **Step 1: Write failing tests**

```python
def test_loopback_defaults_to_mock(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.delenv("LOCAL_DATA_MODE", raising=False)
    assert local_data_mode() == "mock"

def test_loopback_can_explicitly_use_real(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("LOCAL_DATA_MODE", "real")
    assert local_data_mode() == "real"

def test_remote_mock_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("APP_BASE_URL", "https://myai.carher.net")
    monkeypatch.setenv("LOCAL_DATA_MODE", "mock")
    with pytest.raises(RuntimeError, match="LOCAL_DATA_MODE=mock"):
        validate_local_data_mode()
```

- [ ] **Step 2: Run `pytest tests/test_local_data_mode.py -q` and verify the tests fail because the new functions do not exist.**
- [ ] **Step 3: Implement URL parsing, strict mode validation, and lifespan short-circuiting.** Treat malformed/empty mode as `real` for non-loopback and `mock` only for a valid loopback HTTP base URL. Reject `mock` on HTTPS or non-loopback hosts.
- [ ] **Step 4: Run the focused tests and existing `tests/test_toc_auth_policy.py tests/test_remote_demo_read_only.py -q`; verify all pass.**
- [ ] **Step 5: Commit `feat: add safe local mock data mode`.**

### Task 2: 统一本地 Mock 数据容器

**Files:**
- Create: `backend/mock_runtime.py` (extend Task 1)
- Modify: `backend/main.py:316-321, 779-842, 1050-1055`
- Modify: `mock_dev_server.py`
- Test: `tests/test_local_mock_runtime.py`

**Interfaces:**
- `MockRuntime` owns deterministic seed state and exposes `reset()`; all mutable state is process-local.
- `MockClient` implements the subset of `LiteLLMClient` consumed by routes: `models`, `resolve_user`, `user_info`, `keys_for_user_ids`, `available_key_models`, `usage_rows_for_user_ids`, `block_key`, `delete_key`, and safe no-op `close`.
- `MockUsageStore` implements route-consumed usage methods (`connect`, `close`, `health`, `model_usage_counts`, personal/team/member/admin/departments rows) and returns complete payload shapes expected by existing renderers.
- `MockKeyVault` implements `has`, `pending_rotations`, `delete`, and in-memory key reveal/rotation bookkeeping without persisting secrets.

- [ ] **Step 1: Write failing tests for seed shape and mutations.** Assert fixed model names and non-empty usage rows, a token revoke/delete mutation, and `reset()` restoring the original status.
- [ ] **Step 2: Run `pytest tests/test_local_mock_runtime.py -q` and confirm failure on missing runtime classes.**
- [ ] **Step 3: Extract the existing `MockClient`, `MockStore`, and `MockVault` behavior from `mock_dev_server.py` into the runtime module; add deterministic personal/team/admin usage fixtures, model pricing fields, billing-safe identity fields, and reset semantics.
- [ ] **Step 4: Replace `main.client()`, `usage_store()`, and `key_vault()` selection with runtime-aware factories. In mock mode return the singleton runtime components; in real mode preserve current lazy construction unchanged.
- [ ] **Step 5: Make `mock_dev_server.py` set only local environment defaults and call the normal app; remove monkey-patching of `main.client`, `main.usage_store`, and `main.key_vault`.
- [ ] **Step 6: Run focused runtime tests plus `tests/test_key_management.py tests/test_model_catalog_pricing.py tests/test_personal_usage_identity.py -q`; verify pass.**
- [ ] **Step 7: Commit `feat: centralize deterministic local mock runtime`.**

### Task 3: 认证、组织、账单和治理路由接入

**Files:**
- Modify: `backend/main.py:5818-6288, 6474-9000, 11892-12285`
- Modify: `backend/auth_store.py` only if a narrow in-memory adapter is required
- Modify: `backend/billing_store.py` only if a narrow in-memory adapter is required
- Test: `tests/test_local_mock_routes.py`

**Interfaces:**
- Loopback mock `/api/auth/config`, `/api/auth/dev-login`, `/api/auth/me`, `/api/auth/scope`, and logout work without OIDC, SMTP, or auth database.
- Mock route identity is a fixed platform admin plus demo organization members, matching the existing frontend role checks.
- Organization endpoints use `InMemoryOrganizationStore` in mock mode and expose member/department/token/billing mutations in memory.
- Personal billing endpoints return deterministic account/order/redemption payloads and never construct payment URLs or call payment services; unsupported payment settlement returns a clear 409/503 mock-only response.
- Platform/admin stability and cost routes return complete empty-or-seeded structures and keep local CRUD in memory where the frontend submits writes.

- [ ] **Step 1: Write failing route tests using FastAPI `TestClient`: health, dev login, me, models, personal usage, organization current, organization token revoke, billing read, and remote mock rejection.** Assert no real client is instantiated by replacing the real factory with a test sentinel that raises if called.
- [ ] **Step 2: Run `pytest tests/test_local_mock_routes.py -q` and verify failures identify the exact unmocked route dependencies.**
- [ ] **Step 3: Add mock-auth session helpers and route guards so local dev login creates the fixed in-memory identity; keep existing real auth branches unchanged.
- [ ] **Step 4: Route all usage/model/organization calls through the runtime components; remove any “fallback to upstream” branch when `local_mock_enabled()` is true.
- [ ] **Step 5: Add in-memory billing adapter or explicit mock responses for every frontend-called billing route; ensure no payment channel is reported as live.
- [ ] **Step 6: Add safe mock responses for admin stability/cost reads and in-memory mutation handling for frontend-visible CRUD.**
- [ ] **Step 7: Run focused route tests and the complete backend suite; fix only regressions caused by the new mode.**
- [ ] **Step 8: Commit `feat: route all local business APIs through mock data`.**

### Task 4: Configuration, documentation, and compatibility

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `AGENTS.md` only if the documented local startup rule needs clarification
- Test: `tests/test_frontend_local_mock_mode.py` (only if frontend boot behavior changes)

- [ ] **Step 1: Document `LOCAL_DATA_MODE=real` as the only local escape hatch and state that unset loopback mode is mock.** Document process-only mutation lifetime and fixed seed reset on restart.
- [ ] **Step 2: Update local startup examples to use `127.0.0.1:8000` and remove examples that silently use port 8001.**
- [ ] **Step 3: Document supported mock identities and explain that payment/OIDC/upstream side effects are disabled locally.
- [ ] **Step 4: Run markdown/config smoke checks and frontend tests; verify no user-facing LiteLLM/provider wording is introduced.**
- [ ] **Step 5: Commit `docs: document local mock data mode`.**

### Task 5: Full verification and handoff

**Files:**
- Modify: only files required by failing verification
- Test: entire `tests/` suite and local server smoke checks

- [ ] **Step 1: Run `pytest -q` and record the full result.**
- [ ] **Step 2: Start the app on `127.0.0.1:8000` with the repository virtualenv, call `GET /api/health`, perform dev login, load models and usage, mutate one token, then restart and confirm the seed reset.**
- [ ] **Step 3: Run a separate configuration check with `APP_BASE_URL=https://myai.carher.net LOCAL_DATA_MODE=mock` and confirm startup fails before any external worker starts.**
- [ ] **Step 4: Review `git diff`, exclude `.env`, secrets, generated artifacts, virtual environments, `diagnostics/`, and unrelated user changes.**
- [ ] **Step 5: Commit any test-only corrections, then report exact verification evidence.**
- [ ] **Step 6: After the user explicitly requests integration, use the finishing workflow; only after a successful push run the prescribed production sync.**

## Assumptions

- “所有业务接口” means all frontend-invoked authenticated routes, including organization, billing, stability, and cost views; internal observability ingestion and payment callbacks remain disabled or no-op in mock mode.
- Mock data is deterministic per process and resets on service restart; no local JSON/database persistence is added.
- The existing frontend API shapes remain authoritative; mock payloads mirror those shapes rather than adding a frontend-only protocol.
- Production `.env` remains unchanged and continues to select real integrations through its HTTPS `APP_BASE_URL`.

import asyncio

from backend import main
from backend.usage_store import UsageStore


def test_remote_demo_allows_only_snapshot_reads_and_logout(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_DEMO_READ_ONLY", "true")

    assert main.remote_demo_request_allowed("GET", "/api/me/usage") is True
    assert main.remote_demo_request_allowed("GET", "/api/admin/users") is True
    assert main.remote_demo_request_allowed("POST", "/api/auth/logout") is True
    assert main.remote_demo_request_allowed("POST", "/api/me/keys") is False
    assert main.remote_demo_request_allowed("POST", "/api/auth/login") is False
    assert main.remote_demo_request_allowed("GET", "/api/models") is False


def test_remote_demo_uses_configured_snapshot_backends_without_upstream(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_DEMO_READ_ONLY", "true")
    monkeypatch.setenv("USAGE_SNAPSHOT_BACKEND_IDS", "primary, her, primary")
    monkeypatch.setattr(main, "client", lambda: (_ for _ in ()).throw(AssertionError("upstream client used")))

    assert main.usage_backend_ids() == ["primary", "her"]


def test_remote_demo_requires_snapshot_backend_configuration(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_DEMO_READ_ONLY", "true")
    monkeypatch.delenv("USAGE_SNAPSHOT_BACKEND_IDS", raising=False)

    try:
        main.usage_backend_ids()
    except main.HTTPException as exc:
        assert exc.status_code == 503
    else:
        raise AssertionError("missing snapshot backends must fail closed")


def test_read_only_usage_store_skips_schema_and_sets_read_only_transaction(monkeypatch) -> None:
    calls: list[dict] = []

    class Pool:
        async def execute(self, _sql):
            raise AssertionError("schema migration must not run")

    class Asyncpg:
        @staticmethod
        async def create_pool(*_args, **kwargs):
            calls.append(kwargs)
            return Pool()

    monkeypatch.setattr("backend.usage_store.asyncpg", Asyncpg)
    store = UsageStore("postgresql://reader@example/ai_usage", read_only=True)
    asyncio.run(store.connect())

    assert store.pool is not None
    assert calls[0]["server_settings"] == {"default_transaction_read_only": "on"}

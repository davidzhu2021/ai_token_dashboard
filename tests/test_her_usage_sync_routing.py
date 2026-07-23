import asyncio

from backend.usage_sync import UsageSynchronizer


def test_collect_backend_routes_non_primary_accounts_to_their_backend() -> None:
    class Backend:
        id = "her"
        source = "Her"

    class FakeClient:
        backends = [Backend()]

        def _admin_user_map(self, _users):
            return {"her-user": {"name": "Alice", "email": "alice@example.com"}}

        def _is_backend_usage_account(self, _backend, _user_id):
            return True

        async def users(self, _backend):
            return [{"user_id": "her-user", "user_email": "alice@example.com"}]

        async def her_account_index(self, _backend):
            return {"profiles": {"her-user": {"email": "alice@example.com", "name": "Alice"}}}

        def _encode_account_id(self, backend, user_id):
            return user_id if backend.id == "primary" else f"{backend.id}:{user_id}"

        async def usage_rows(self, user_id, _start_date, _end_date, _source):
            assert user_id == "her:her-user"
            return [{"date": "2026-07-23", "source": "Her", "model": "her-model", "totalTokens": 42}]

        async def teams(self, _backend):
            return []

    snapshot = asyncio.run(UsageSynchronizer(FakeClient(), object()).collect_backend(Backend(), "2026-07-21", "2026-07-23"))

    assert snapshot.rows[0]["source"] == "Her"
    assert snapshot.rows[0]["_userId"] == "her-user"

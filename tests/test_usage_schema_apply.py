import asyncio

from backend.usage_store import USAGE_SCHEMA, apply_usage_schema, split_sql_statements


def test_split_sql_statements_does_not_keep_schema_as_one_transaction() -> None:
    statements = split_sql_statements(USAGE_SCHEMA)

    assert len(statements) > 20
    assert statements[0].startswith("CREATE TABLE IF NOT EXISTS usage_daily")
    assert not any(
        "CREATE TABLE IF NOT EXISTS usage_daily" in item
        and "CREATE TABLE IF NOT EXISTS usage_sync_coverage" in item
        for item in statements
    )


def test_split_sql_statements_keeps_dollar_quoted_do_block_together() -> None:
    statements = split_sql_statements(USAGE_SCHEMA)
    matching = [item for item in statements if "cost_items_plan_version_fk" in item]

    assert len(matching) == 1
    assert "DO $$" in matching[0]
    assert "END $$" in matching[0]
    assert matching[0].count(";") >= 2


def test_apply_usage_schema_sets_lock_timeout_and_executes_each_statement() -> None:
    executed: list[str] = []

    class Connection:
        async def execute(self, sql, *args):
            executed.append(sql if not args else f"{sql}:{args[0]}")
            return "OK"

    class Pool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    schema = """
    CREATE TABLE IF NOT EXISTS usage_daily (id TEXT);
    CREATE TABLE IF NOT EXISTS usage_sync_coverage (id TEXT);
    """
    asyncio.run(apply_usage_schema(Pool(), schema))

    assert executed[0].startswith("SELECT set_config('lock_timeout'")
    assert "CREATE TABLE IF NOT EXISTS usage_daily" in executed[1]
    assert "CREATE TABLE IF NOT EXISTS usage_sync_coverage" in executed[2]
    assert len(executed) == 3

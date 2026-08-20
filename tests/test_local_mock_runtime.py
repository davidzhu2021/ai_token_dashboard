import asyncio

from backend.mock_runtime import MockRuntime


def test_mock_runtime_has_models_usage_and_resettable_keys():
    runtime = MockRuntime()

    models = asyncio.run(runtime.client.models())
    rows = asyncio.run(
        runtime.client.usage_rows_for_user_ids(
            ["primary:owner"], "2026-08-01", "2026-08-03", "all"
        )
    )
    keys = asyncio.run(runtime.client.keys_for_user_ids(["primary:owner"]))

    assert {item["displayName"] for item in models} >= {"gpt-5.2", "claude-sonnet-4-6"}
    assert rows and rows[0]["totalTokens"] > 0
    assert keys[0]["status"] == "正常"

    asyncio.run(runtime.client.block_key(keys[0]["id"], "primary:owner", "owner@demo.example"))
    assert asyncio.run(runtime.client.keys_for_user_ids(["primary:owner"]))[0]["status"] == "已禁用"

    runtime.reset()
    assert asyncio.run(runtime.client.keys_for_user_ids(["primary:owner"]))[0]["status"] == "正常"

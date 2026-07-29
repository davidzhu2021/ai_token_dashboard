"""充值账本的真实数据库测试。

落账幂等、兑换码 CAS、行锁这些语义全在 SQL 里，用假连接池测等于测 mock
自己。因此这里连真实 Postgres：配置 ``BILLING_TEST_DATABASE_URL`` 即启用，
未配置则整个模块跳过，不阻塞没有数据库的开发环境。

本地起一个临时库：

    docker run -d --name billing-test-pg -e POSTGRES_PASSWORD=testpw \\
        -e POSTGRES_DB=billing_test -p 15432:5432 postgres:16-alpine
    export BILLING_TEST_DATABASE_URL=postgresql://postgres:testpw@127.0.0.1:15432/billing_test
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from backend.billing_store import (
    BillingStore,
    BillingStoreError,
    CHANNEL_EPAY,
    CHANNEL_MANUAL_QR,
    CHANNEL_REDEMPTION,
    ORDER_FAILED,
    ORDER_PENDING,
    ORDER_SUCCESS,
    REDEMPTION_DISABLED,
    REDEMPTION_USED,
    SYNC_DONE,
    SYNC_PENDING,
    generate_redemption_code,
    redemption_code_hash,
)

TEST_DSN = os.getenv("BILLING_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="需要 BILLING_TEST_DATABASE_URL 指向可写的测试 PostgreSQL",
)


async def _fresh_store() -> BillingStore:
    # 并发用例需要多条连接，但整个文件跑下来不能耗尽库的连接上限。
    store = BillingStore(TEST_DSN, min_size=1, max_size=6)
    await store.connect()
    await store.pool.execute(
        "TRUNCATE billing_order, billing_account, billing_redemption"
    )
    return store


def run(coro):
    return asyncio.run(coro)


# ---- 落账幂等 ----


def test_settle_order_credits_balance_once() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("T1", "u1", CHANNEL_EPAY, 10.0, 73.0, 7.3, "alipay")

            first = await store.settle_order("T1", upstream_trade_no="gw-1")
            assert first["settled"] is True
            assert first["account"]["balanceUsd"] == pytest.approx(10.0)
            assert first["order"]["status"] == ORDER_SUCCESS

            # 支付网关重推：必须返回成功语义但不能重复加钱。
            second = await store.settle_order("T1", upstream_trade_no="gw-1")
            assert second["settled"] is False
            assert second["account"]["balanceUsd"] == pytest.approx(10.0)

            account = await store.get_account("u1")
            assert account["balanceUsd"] == pytest.approx(10.0)
            assert account["topupTotalUsd"] == pytest.approx(10.0)
        finally:
            await store.close()

    run(scenario())


def test_concurrent_settle_credits_once() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("T2", "u2", CHANNEL_EPAY, 25.0, 182.5, 7.3, "wxpay")

            results = await asyncio.gather(*(store.settle_order("T2") for _ in range(5)))

            assert sum(1 for item in results if item["settled"]) == 1
            account = await store.get_account("u2")
            assert account["balanceUsd"] == pytest.approx(25.0)
        finally:
            await store.close()

    run(scenario())


def test_settle_accumulates_across_orders() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("T3", "u3", CHANNEL_EPAY, 10.0, 73.0, 7.3)
            await store.create_order("T4", "u3", CHANNEL_EPAY, 5.5, 40.15, 7.3)
            await store.settle_order("T3")
            await store.settle_order("T4")

            account = await store.get_account("u3")
            assert account["balanceUsd"] == pytest.approx(15.5)
            assert account["topupTotalUsd"] == pytest.approx(15.5)
        finally:
            await store.close()

    run(scenario())


def test_settle_unknown_order_raises() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            with pytest.raises(BillingStoreError):
                await store.settle_order("does-not-exist")
        finally:
            await store.close()

    run(scenario())


def test_failed_order_cannot_be_settled() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("T5", "u5", CHANNEL_EPAY, 30.0, 219.0, 7.3)
            await store.fail_order("T5", "用户取消")

            result = await store.settle_order("T5")

            assert result["settled"] is False
            assert result["order"]["status"] == ORDER_FAILED
            account = await store.get_account("u5")
            assert account["balanceUsd"] == pytest.approx(0.0)
        finally:
            await store.close()

    run(scenario())


def test_settle_marks_sync_pending_for_upstream_write() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("T6", "u6", CHANNEL_EPAY, 12.0, 87.6, 7.3)
            result = await store.settle_order("T6")

            assert result["order"]["syncState"] == SYNC_PENDING
            assert await store.pending_sync_count() == 1
            pending = await store.pending_sync_orders()
            assert [item["tradeNo"] for item in pending] == ["T6"]

            await store.mark_sync_state("T6", SYNC_DONE)
            assert await store.pending_sync_count() == 0
        finally:
            await store.close()

    run(scenario())


def test_sync_failure_keeps_balance_and_records_error() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("T7", "u7", CHANNEL_EPAY, 20.0, 146.0, 7.3)
            await store.settle_order("T7")

            # 钱已收到，上游写失败绝不能回滚本地账本。
            await store.mark_sync_state("T7", SYNC_PENDING, "上游 503")

            account = await store.get_account("u7")
            assert account["balanceUsd"] == pytest.approx(20.0)
            order = await store.get_order("T7")
            assert order["status"] == ORDER_SUCCESS
            assert order["syncError"] == "上游 503"
            assert await store.pending_sync_count() == 1
        finally:
            await store.close()

    run(scenario())


# ---- 收款码转账：提交凭证与人工确认 ----


def test_submit_manual_payment_moves_order_into_review_queue() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("M1", "u10", CHANNEL_MANUAL_QR, 10.0, 73.0, 7.3, "alipay")
            assert await store.pending_review_count() == 0

            order = await store.submit_manual_payment("M1", "u10", "尾号 1234")

            # 提交凭证不改状态，仅进入待确认队列：额度必须等管理员确认。
            assert order["status"] == ORDER_PENDING
            assert order["payerNote"] == "尾号 1234"
            assert order["submittedAt"] != ""
            assert await store.pending_review_count() == 1
            assert [item["tradeNo"] for item in await store.list_pending_reviews()] == ["M1"]

            account = await store.get_account("u10")
            assert account["balanceUsd"] == pytest.approx(0.0)
        finally:
            await store.close()

    run(scenario())


def test_submit_manual_payment_rejects_other_users_order() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("M2", "owner", CHANNEL_MANUAL_QR, 10.0, 73.0, 7.3, "alipay")

            with pytest.raises(BillingStoreError):
                await store.submit_manual_payment("M2", "attacker", "我付过了")

            order = await store.get_order("M2")
            assert order["payerNote"] == ""
            assert order["submittedAt"] == ""
        finally:
            await store.close()

    run(scenario())


def test_submit_manual_payment_rejects_settled_order() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("M3", "u11", CHANNEL_MANUAL_QR, 10.0, 73.0, 7.3, "alipay")
            await store.settle_order("M3")

            # 已到账的订单不能再被"重新提交"，否则会造成重复核对与重复放款。
            with pytest.raises(BillingStoreError):
                await store.submit_manual_payment("M3", "u11", "再提交一次")
        finally:
            await store.close()

    run(scenario())


def test_resubmitting_manual_payment_overwrites_note() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("M4", "u12", CHANNEL_MANUAL_QR, 10.0, 73.0, 7.3, "wxpay")
            await store.submit_manual_payment("M4", "u12", "填错了")
            order = await store.submit_manual_payment("M4", "u12", "流水号 998877")

            # 允许改正填错的流水号，但不能因此变成两笔待确认。
            assert order["payerNote"] == "流水号 998877"
            assert await store.pending_review_count() == 1
        finally:
            await store.close()

    run(scenario())


def test_manual_confirmation_records_reviewer_and_credits_once() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("M5", "u13", CHANNEL_MANUAL_QR, 25.0, 182.5, 7.3, "alipay")
            await store.submit_manual_payment("M5", "u13", "尾号 4321")

            first = await store.settle_order(
                "M5", reviewed_by="boss@company.test", review_note="已核对收款"
            )
            assert first["settled"] is True
            assert first["order"]["reviewedBy"] == "boss@company.test"
            assert first["order"]["reviewNote"] == "已核对收款"
            assert first["account"]["balanceUsd"] == pytest.approx(25.0)
            assert await store.pending_review_count() == 0

            # 管理员误点两次不能重复放款。
            second = await store.settle_order("M5", reviewed_by="boss@company.test")
            assert second["settled"] is False
            account = await store.get_account("u13")
            assert account["balanceUsd"] == pytest.approx(25.0)
        finally:
            await store.close()

    run(scenario())


def test_rejecting_manual_order_records_reason_and_leaves_balance() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("M6", "u14", CHANNEL_MANUAL_QR, 10.0, 73.0, 7.3, "alipay")
            await store.submit_manual_payment("M6", "u14", "转错账了")

            assert await store.fail_order("M6", "未查到该笔付款", reviewed_by="boss@company.test") is True
            # 已驳回的订单不能再次驳回，避免覆盖第一次的处理记录。
            assert await store.fail_order("M6", "再驳一次") is False

            order = await store.get_order("M6")
            assert order["status"] == ORDER_FAILED
            assert order["reviewNote"] == "未查到该笔付款"
            assert order["reviewedBy"] == "boss@company.test"
            assert await store.pending_review_count() == 0
            account = await store.get_account("u14")
            assert account["balanceUsd"] == pytest.approx(0.0)
        finally:
            await store.close()

    run(scenario())


def test_unsubmitted_manual_order_stays_out_of_review_queue() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("M7", "u15", CHANNEL_MANUAL_QR, 10.0, 73.0, 7.3, "alipay")

            # 只下单没付款的订单不该占用管理员的待办列表。
            assert await store.pending_review_count() == 0
            assert await store.list_pending_reviews() == []
        finally:
            await store.close()

    run(scenario())


# ---- 兑换码 ----


def test_redeem_credits_balance_and_writes_order() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            created = await store.create_redemptions(1, 50.0, name="测试批次", created_by="admin@x")
            code = created[0]["code"]

            result = await store.redeem(code, "u10")

            assert result["amountUsd"] == pytest.approx(50.0)
            assert result["account"]["balanceUsd"] == pytest.approx(50.0)

            # 兑换要与在线支付共用同一份流水口径。
            orders = await store.list_user_orders("u10")
            assert orders["total"] == 1
            assert orders["items"][0]["channel"] == CHANNEL_REDEMPTION
            assert orders["items"][0]["status"] == ORDER_SUCCESS
            assert orders["items"][0]["syncState"] == SYNC_PENDING
        finally:
            await store.close()

    run(scenario())


def test_redeem_twice_rejects_second_attempt() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            code = (await store.create_redemptions(1, 10.0))[0]["code"]
            await store.redeem(code, "u11")

            with pytest.raises(BillingStoreError, match="已被使用"):
                await store.redeem(code, "u12")

            assert (await store.get_account("u12"))["balanceUsd"] == pytest.approx(0.0)
        finally:
            await store.close()

    run(scenario())


def test_concurrent_redeem_only_one_wins() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            code = (await store.create_redemptions(1, 100.0))[0]["code"]

            results = await asyncio.gather(
                *(store.redeem(code, f"user-{index}") for index in range(6)),
                return_exceptions=True,
            )

            wins = [item for item in results if not isinstance(item, Exception)]
            assert len(wins) == 1
            assert all(isinstance(item, BillingStoreError) for item in results if isinstance(item, Exception))

            # 总额度只发出去一份。
            total = await store.pool.fetchval("SELECT coalesce(sum(balance_usd), 0) FROM billing_account")
            assert float(total) == pytest.approx(100.0)
        finally:
            await store.close()

    run(scenario())


def test_redeem_unknown_code_rejected() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            with pytest.raises(BillingStoreError, match="无效"):
                await store.redeem("ZZZZZZZZZZZZZZZZZZZZ", "u13")
        finally:
            await store.close()

    run(scenario())


def test_redeem_blank_code_rejected() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            with pytest.raises(BillingStoreError, match="请输入兑换码"):
                await store.redeem("   ", "u13")
        finally:
            await store.close()

    run(scenario())


def test_redeem_disabled_code_rejected() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            created = await store.create_redemptions(1, 10.0)
            assert await store.disable_redemption(created[0]["id"]) is True

            with pytest.raises(BillingStoreError, match="已停用"):
                await store.redeem(created[0]["code"], "u14")
        finally:
            await store.close()

    run(scenario())


def test_redeem_expired_code_rejected() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
            created = await store.create_redemptions(1, 10.0, expires_at=expired_at)

            with pytest.raises(BillingStoreError, match="已过期"):
                await store.redeem(created[0]["code"], "u15")
        finally:
            await store.close()

    run(scenario())


def test_redeem_accepts_lowercase_and_padded_input() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            code = (await store.create_redemptions(1, 7.0))[0]["code"]

            result = await store.redeem(f"  {code.lower()}  ", "u16")

            assert result["account"]["balanceUsd"] == pytest.approx(7.0)
        finally:
            await store.close()

    run(scenario())


def test_redemption_plaintext_is_never_persisted() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            code = (await store.create_redemptions(1, 10.0, name="批次"))[0]["code"]

            row = await store.pool.fetchrow(
                "SELECT code_hash, code_hint FROM billing_redemption LIMIT 1"
            )
            assert row["code_hash"] == redemption_code_hash(code)
            assert code not in row["code_hash"]
            assert row["code_hint"] == code[-4:]

            listed = await store.list_redemptions()
            # 管理端列表只应暴露尾 4 位，不能回显明文。
            assert listed["items"][0]["codeHint"] == code[-4:]
            assert "code" not in listed["items"][0]
        finally:
            await store.close()

    run(scenario())


def test_disable_used_redemption_is_noop() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            created = await store.create_redemptions(1, 10.0)
            await store.redeem(created[0]["code"], "u17")

            assert await store.disable_redemption(created[0]["id"]) is False
            listed = await store.list_redemptions()
            assert listed["items"][0]["status"] == REDEMPTION_USED
        finally:
            await store.close()

    run(scenario())


def test_batch_creation_generates_distinct_codes() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            created = await store.create_redemptions(20, 5.0, name="批量")

            codes = {item["code"] for item in created}
            assert len(codes) == 20
            listed = await store.list_redemptions(limit=50)
            assert listed["total"] == 20
            assert all(item["status"] == "enabled" for item in listed["items"])
        finally:
            await store.close()

    run(scenario())


# ---- 订单查询 ----


def test_user_orders_are_scoped_to_owner() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("A1", "owner", CHANNEL_EPAY, 10.0, 73.0, 7.3)
            await store.create_order("B1", "other", CHANNEL_EPAY, 20.0, 146.0, 7.3)

            mine = await store.list_user_orders("owner")

            assert [item["tradeNo"] for item in mine["items"]] == ["A1"]
            assert mine["total"] == 1
        finally:
            await store.close()

    run(scenario())


def test_admin_order_search_matches_trade_no() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("FIND-ME", "u20", CHANNEL_EPAY, 10.0, 73.0, 7.3)
            await store.create_order("OTHER", "u21", CHANNEL_EPAY, 10.0, 73.0, 7.3)

            found = await store.list_all_orders(keyword="find")
            assert [item["tradeNo"] for item in found["items"]] == ["FIND-ME"]

            everything = await store.list_all_orders()
            assert everything["total"] == 2
        finally:
            await store.close()

    run(scenario())


def test_order_keeps_exchange_rate_snapshot() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            await store.create_order("RATE1", "u22", CHANNEL_EPAY, 10.0, 73.0, 7.3, "alipay")

            order = await store.get_order("RATE1")

            # 汇率快照必须留在订单上，事后改汇率不能改写历史账目。
            assert order["exchangeRate"] == pytest.approx(7.3)
            assert order["moneyCny"] == pytest.approx(73.0)
            assert order["amountUsd"] == pytest.approx(10.0)
            assert order["paymentMethod"] == "alipay"
            assert order["status"] == ORDER_PENDING
        finally:
            await store.close()

    run(scenario())


def test_get_account_for_unknown_user_is_zero() -> None:
    async def scenario() -> None:
        store = await _fresh_store()
        try:
            account = await store.get_account("nobody")

            assert account["balanceUsd"] == pytest.approx(0.0)
            assert account["topupTotalUsd"] == pytest.approx(0.0)
        finally:
            await store.close()

    run(scenario())


# ---- 纯函数 ----


def test_generated_codes_avoid_ambiguous_characters() -> None:
    for _ in range(50):
        code = generate_redemption_code()
        assert len(code) == 20
        assert not set(code) & set("01OIL")


def test_code_hash_is_salted_when_secret_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BILLING_REDEMPTION_SECRET", raising=False)
    unsalted = redemption_code_hash("ABCD")

    monkeypatch.setenv("BILLING_REDEMPTION_SECRET", "pepper")
    salted = redemption_code_hash("ABCD")

    assert unsalted != salted
    # 大小写与空白归一化必须稳定，否则用户抄写的兑换码会认不出。
    assert redemption_code_hash(" abcd ") == salted


def test_store_disabled_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BILLING_ENABLED", "false")
    monkeypatch.setenv("USAGE_DATABASE_URL", TEST_DSN)
    assert BillingStore.from_environment() is None

    monkeypatch.setenv("BILLING_ENABLED", "true")
    monkeypatch.setenv("USAGE_DATABASE_URL", "")
    assert BillingStore.from_environment() is None

    monkeypatch.setenv("USAGE_DATABASE_URL", TEST_DSN)
    assert BillingStore.from_environment() is not None

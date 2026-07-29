"""充值账本与订单存储。

余额以本地账本为真相源，上游额度只是同步出去的执行副本。所有渠道
（兑换码、在线支付回调、管理员补单）都必须经由 :meth:`BillingStore.settle_order`
落账，幂等只在那一处实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

try:  # pragma: no cover - import guard mirrors usage_store
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover
    asyncpg = None  # type: ignore[assignment]


BILLING_SCHEMA = """
CREATE TABLE IF NOT EXISTS billing_account (
    user_id TEXT PRIMARY KEY,
    balance_usd NUMERIC(16,6) NOT NULL DEFAULT 0,
    topup_total_usd NUMERIC(16,6) NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS billing_order (
    trade_no TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    amount_usd NUMERIC(16,6) NOT NULL,
    money_cny NUMERIC(12,2) NOT NULL DEFAULT 0,
    exchange_rate NUMERIC(10,4) NOT NULL,
    status TEXT NOT NULL,
    payment_method TEXT NOT NULL DEFAULT '',
    upstream_trade_no TEXT NOT NULL DEFAULT '',
    notify_payload JSONB,
    sync_state TEXT NOT NULL DEFAULT '',
    sync_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);

-- 收款码转账渠道需要用户回填付款凭证，老库升级时补列。
ALTER TABLE billing_order ADD COLUMN IF NOT EXISTS payer_note TEXT NOT NULL DEFAULT '';
ALTER TABLE billing_order ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ;
ALTER TABLE billing_order ADD COLUMN IF NOT EXISTS review_note TEXT NOT NULL DEFAULT '';
ALTER TABLE billing_order ADD COLUMN IF NOT EXISTS reviewed_by TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS billing_order_user_idx
    ON billing_order (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS billing_order_status_idx
    ON billing_order (status, created_at);
CREATE INDEX IF NOT EXISTS billing_order_sync_idx
    ON billing_order (sync_state) WHERE sync_state = 'pending';

CREATE TABLE IF NOT EXISTS billing_redemption (
    id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    code_hint TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    amount_usd NUMERIC(16,6) NOT NULL,
    status TEXT NOT NULL DEFAULT 'enabled',
    created_by TEXT NOT NULL DEFAULT '',
    used_by TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS billing_redemption_status_idx
    ON billing_redemption (status, created_at DESC);
"""


ORDER_PENDING = "pending"
ORDER_SUCCESS = "success"
ORDER_FAILED = "failed"
ORDER_EXPIRED = "expired"

REDEMPTION_ENABLED = "enabled"
REDEMPTION_USED = "used"
REDEMPTION_DISABLED = "disabled"

CHANNEL_REDEMPTION = "redemption"
CHANNEL_EPAY = "epay"
CHANNEL_MANUAL = "manual"
CHANNEL_MANUAL_QR = "manual_qr"

# 收款码订单没有独立的"待审核"状态：它仍是 pending，只是 submitted_at 非空。
# 这样落账仍走 settle_order 上那一处 pending -> success 的 CAS，不必新增状态机分支。

SYNC_PENDING = "pending"
SYNC_DONE = "done"

ORDER_COLUMNS = """
    trade_no, user_id, channel, amount_usd, money_cny, exchange_rate,
    status, payment_method, upstream_trade_no, sync_state, sync_error,
    payer_note, review_note, reviewed_by, submitted_at,
    created_at, completed_at
"""


class BillingStoreError(RuntimeError):
    """账本操作失败。"""


def redemption_code_hash(code: str) -> str:
    """派生兑换码哈希。

    只落哈希，明文仅在生成时返回一次，避免库泄露即等于资金泄露。加盐取自
    ``BILLING_REDEMPTION_SECRET``，未配置时退回无盐 sha256——仍不可逆，但
    生产应当配置以抵御彩虹表。
    """
    normalized = str(code or "").strip().upper()
    secret = os.getenv("BILLING_REDEMPTION_SECRET", "").strip()
    if secret:
        return hmac.new(secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_redemption_code() -> str:
    """生成 20 位无歧义字符兑换码。

    去掉 0/O/1/I/L 等易混字符，减少人工转录出错。
    """
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(20))


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _money(value: Any) -> float:
    return float(_as_decimal(value))


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return ""


class BillingStore:
    """充值账本的 PostgreSQL 适配层，与用量快照同库不同表。"""

    def __init__(self, dsn: str, min_size: int = 1, max_size: int = 5) -> None:
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.pool: Any = None
        self._connect_lock = asyncio.Lock()

    @classmethod
    def from_environment(cls) -> BillingStore | None:
        dsn = os.getenv("USAGE_DATABASE_URL", "").strip()
        enabled = os.getenv("BILLING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled or not dsn:
            return None
        return cls(dsn)

    async def connect(self) -> None:
        if self.pool is not None:
            return
        if asyncpg is None:
            raise RuntimeError("BILLING_ENABLED=true 时需要安装 asyncpg")
        async with self._connect_lock:
            if self.pool is not None:
                return
            pool = await asyncpg.create_pool(
                self.dsn, min_size=self.min_size, max_size=self.max_size, command_timeout=30
            )
            try:
                await pool.execute(BILLING_SCHEMA)
            except Exception:
                await pool.close()
                raise
            self.pool = pool

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _require_pool(self) -> Any:
        if self.pool is None:
            raise RuntimeError("充值数据库尚未连接")
        return self.pool

    # ---- 账户 ----

    async def get_account(self, user_id: str) -> dict[str, Any]:
        row = await self._require_pool().fetchrow(
            """
            SELECT user_id, balance_usd, topup_total_usd, updated_at
            FROM billing_account WHERE user_id = $1
            """,
            str(user_id),
        )
        if row is None:
            return {"userId": str(user_id), "balanceUsd": 0.0, "topupTotalUsd": 0.0, "updatedAt": ""}
        return {
            "userId": str(row["user_id"]),
            "balanceUsd": _money(row["balance_usd"]),
            "topupTotalUsd": _money(row["topup_total_usd"]),
            "updatedAt": _iso(row["updated_at"]),
        }

    # ---- 订单 ----

    async def create_order(
        self,
        trade_no: str,
        user_id: str,
        channel: str,
        amount_usd: float,
        money_cny: float,
        exchange_rate: float,
        payment_method: str = "",
        status: str = ORDER_PENDING,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        try:
            row = await self._require_pool().fetchrow(
                """
                INSERT INTO billing_order (
                    trade_no, user_id, channel, amount_usd, money_cny,
                    exchange_rate, status, payment_method, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING trade_no, status
                """,
                str(trade_no),
                str(user_id),
                str(channel),
                _as_decimal(amount_usd),
                _as_decimal(money_cny),
                _as_decimal(exchange_rate),
                str(status),
                str(payment_method or ""),
                now,
            )
        except Exception as exc:  # pragma: no cover - 依赖真实驱动的唯一键冲突
            raise BillingStoreError("创建充值订单失败") from exc
        return {"tradeNo": str(row["trade_no"]), "status": str(row["status"])}

    async def get_order(self, trade_no: str) -> dict[str, Any] | None:
        row = await self._require_pool().fetchrow(
            f"SELECT {ORDER_COLUMNS} FROM billing_order WHERE trade_no = $1",
            str(trade_no),
        )
        return self._order_payload(row) if row is not None else None

    def _order_payload(self, row: Any) -> dict[str, Any]:
        return {
            "tradeNo": str(row["trade_no"]),
            "userId": str(row["user_id"]),
            "channel": str(row["channel"]),
            "amountUsd": _money(row["amount_usd"]),
            "moneyCny": _money(row["money_cny"]),
            "exchangeRate": _money(row["exchange_rate"]),
            "status": str(row["status"]),
            "paymentMethod": str(row["payment_method"] or ""),
            "upstreamTradeNo": str(row["upstream_trade_no"] or ""),
            "syncState": str(row["sync_state"] or ""),
            "syncError": str(row["sync_error"] or ""),
            "payerNote": str(row["payer_note"] or ""),
            "reviewNote": str(row["review_note"] or ""),
            "reviewedBy": str(row["reviewed_by"] or ""),
            "submittedAt": _iso(row["submitted_at"]),
            "createdAt": _iso(row["created_at"]),
            "completedAt": _iso(row["completed_at"]),
        }

    async def list_user_orders(self, user_id: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        pool = self._require_pool()
        capped = max(1, min(200, int(limit)))
        rows = await pool.fetch(
            f"""
            SELECT {ORDER_COLUMNS}
            FROM billing_order
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            str(user_id),
            capped,
            max(0, int(offset)),
        )
        total = await pool.fetchval("SELECT count(*) FROM billing_order WHERE user_id = $1", str(user_id))
        return {"items": [self._order_payload(row) for row in rows], "total": int(total or 0)}

    async def list_all_orders(
        self, keyword: str = "", limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        pool = self._require_pool()
        capped = max(1, min(200, int(limit)))
        needle = str(keyword or "").strip()
        columns = ORDER_COLUMNS
        if needle:
            pattern = f"%{needle}%"
            rows = await pool.fetch(
                f"""
                SELECT {columns} FROM billing_order
                WHERE trade_no ILIKE $1 OR user_id ILIKE $1
                ORDER BY created_at DESC LIMIT $2 OFFSET $3
                """,
                pattern,
                capped,
                max(0, int(offset)),
            )
            total = await pool.fetchval(
                "SELECT count(*) FROM billing_order WHERE trade_no ILIKE $1 OR user_id ILIKE $1",
                pattern,
            )
        else:
            rows = await pool.fetch(
                f"""
                SELECT {columns} FROM billing_order
                ORDER BY created_at DESC LIMIT $1 OFFSET $2
                """,
                capped,
                max(0, int(offset)),
            )
            total = await pool.fetchval("SELECT count(*) FROM billing_order")
        return {"items": [self._order_payload(row) for row in rows], "total": int(total or 0)}

    # ---- 落账：所有渠道的唯一入口 ----

    async def settle_order(
        self,
        trade_no: str,
        upstream_trade_no: str = "",
        notify_payload: str | None = None,
        reviewed_by: str = "",
        review_note: str = "",
    ) -> dict[str, Any]:
        """把一笔待付订单结算入账，重复调用不会重复加钱。

        返回 ``{"settled": bool, "order": {...}, "account": {...}}``。``settled``
        为 False 表示这次调用没有真正落账（订单已结算或已失效），调用方应当把它
        当成成功响应而不是错误——支付网关重推回调时必须拿到成功语义，否则会
        无限重试。

        ``reviewed_by``/``review_note`` 供人工确认渠道留痕，自动回调不传。
        """
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                order = await connection.fetchrow(
                    """
                    SELECT trade_no, user_id, amount_usd, status
                    FROM billing_order WHERE trade_no = $1 FOR UPDATE
                    """,
                    str(trade_no),
                )
                if order is None:
                    raise BillingStoreError("充值订单不存在")
                # CAS：只有仍处于 pending 的订单才会被这一次调用推进。并发或重推
                # 的第二个请求 rowcount 为 0，据此判定"已处理"而非报错。
                updated = await connection.execute(
                    """
                    UPDATE billing_order
                    SET status = $2, completed_at = $3, upstream_trade_no = $4,
                        notify_payload = COALESCE($5::jsonb, notify_payload),
                        sync_state = $6,
                        reviewed_by = COALESCE(NULLIF($8, ''), reviewed_by),
                        review_note = COALESCE(NULLIF($9, ''), review_note)
                    WHERE trade_no = $1 AND status = $7
                    """,
                    str(trade_no),
                    ORDER_SUCCESS,
                    datetime.now(timezone.utc),
                    str(upstream_trade_no or ""),
                    notify_payload,
                    SYNC_PENDING,
                    ORDER_PENDING,
                    str(reviewed_by or "")[:200],
                    str(review_note or "")[:500],
                )
                if not str(updated).endswith(" 1"):
                    fresh = await connection.fetchrow(
                        f"SELECT {ORDER_COLUMNS} FROM billing_order WHERE trade_no = $1",
                        str(trade_no),
                    )
                    account = await self._account_in_tx(connection, str(order["user_id"]))
                    return {"settled": False, "order": self._order_payload(fresh), "account": account}

                amount = _as_decimal(order["amount_usd"])
                now = datetime.now(timezone.utc)
                account_row = await connection.fetchrow(
                    """
                    INSERT INTO billing_account (user_id, balance_usd, topup_total_usd, created_at, updated_at)
                    VALUES ($1, $2, $2, $3, $3)
                    ON CONFLICT (user_id) DO UPDATE
                    SET balance_usd = billing_account.balance_usd + $2,
                        topup_total_usd = billing_account.topup_total_usd + $2,
                        updated_at = $3
                    RETURNING user_id, balance_usd, topup_total_usd, updated_at
                    """,
                    str(order["user_id"]),
                    amount,
                    now,
                )
                settled = await connection.fetchrow(
                    f"SELECT {ORDER_COLUMNS} FROM billing_order WHERE trade_no = $1",
                    str(trade_no),
                )
        return {
            "settled": True,
            "order": self._order_payload(settled),
            "account": {
                "userId": str(account_row["user_id"]),
                "balanceUsd": _money(account_row["balance_usd"]),
                "topupTotalUsd": _money(account_row["topup_total_usd"]),
                "updatedAt": _iso(account_row["updated_at"]),
            },
        }

    async def _account_in_tx(self, connection: Any, user_id: str) -> dict[str, Any]:
        row = await connection.fetchrow(
            "SELECT user_id, balance_usd, topup_total_usd, updated_at FROM billing_account WHERE user_id = $1",
            str(user_id),
        )
        if row is None:
            return {"userId": str(user_id), "balanceUsd": 0.0, "topupTotalUsd": 0.0, "updatedAt": ""}
        return {
            "userId": str(row["user_id"]),
            "balanceUsd": _money(row["balance_usd"]),
            "topupTotalUsd": _money(row["topup_total_usd"]),
            "updatedAt": _iso(row["updated_at"]),
        }

    async def fail_order(
        self, trade_no: str, reason: str = "", reviewed_by: str = ""
    ) -> bool:
        result = await self._require_pool().execute(
            """
            UPDATE billing_order
            SET status = $2, sync_error = $3,
                review_note = $3,
                reviewed_by = COALESCE(NULLIF($5, ''), reviewed_by),
                completed_at = $6
            WHERE trade_no = $1 AND status = $4
            """,
            str(trade_no),
            ORDER_FAILED,
            str(reason or "")[:500],
            ORDER_PENDING,
            str(reviewed_by or "")[:200],
            datetime.now(timezone.utc),
        )
        return str(result).endswith(" 1")

    # ---- 收款码转账：用户回填凭证 + 管理员确认 ----

    async def submit_manual_payment(
        self, trade_no: str, user_id: str, payer_note: str
    ) -> dict[str, Any]:
        """记录用户提交的付款说明，把订单推入人工待确认队列。

        只允许订单本人、且仅当订单仍处于 ``pending``；重复提交会覆盖上一次说明，
        便于用户改正填错的流水号。
        """
        row = await self._require_pool().fetchrow(
            f"""
            UPDATE billing_order
            SET payer_note = $3, submitted_at = $4
            WHERE trade_no = $1 AND user_id = $2 AND status = $5
            RETURNING {ORDER_COLUMNS}
            """,
            str(trade_no),
            str(user_id),
            str(payer_note or "")[:500],
            datetime.now(timezone.utc),
            ORDER_PENDING,
        )
        if row is None:
            raise BillingStoreError("订单不存在或已处理，无法提交付款凭证")
        return self._order_payload(row)

    async def list_pending_reviews(self, limit: int = 50) -> list[dict[str, Any]]:
        """已提交凭证但尚未确认的订单，管理员优先处理这一批。"""
        rows = await self._require_pool().fetch(
            f"""
            SELECT {ORDER_COLUMNS}
            FROM billing_order
            WHERE status = $1 AND submitted_at IS NOT NULL
            ORDER BY submitted_at
            LIMIT $2
            """,
            ORDER_PENDING,
            max(1, min(200, int(limit))),
        )
        return [self._order_payload(row) for row in rows]

    async def pending_review_count(self) -> int:
        value = await self._require_pool().fetchval(
            "SELECT count(*) FROM billing_order WHERE status = $1 AND submitted_at IS NOT NULL",
            ORDER_PENDING,
        )
        return int(value or 0)

    async def mark_sync_state(self, trade_no: str, state: str, error: str = "") -> None:
        """记录上游额度同步结果。

        上游写入失败不回滚本地账本——钱已经收到了。这里只留标记，由补偿逻辑
        重试，并在健康检查里暴露待同步计数。
        """
        await self._require_pool().execute(
            "UPDATE billing_order SET sync_state = $2, sync_error = $3 WHERE trade_no = $1",
            str(trade_no),
            str(state),
            str(error or "")[:500],
        )

    async def pending_sync_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self._require_pool().fetch(
            f"""
            SELECT {ORDER_COLUMNS}
            FROM billing_order
            WHERE sync_state = $1 AND status = $2
            ORDER BY completed_at
            LIMIT $3
            """,
            SYNC_PENDING,
            ORDER_SUCCESS,
            max(1, min(500, int(limit))),
        )
        return [self._order_payload(row) for row in rows]

    async def pending_sync_count(self) -> int:
        value = await self._require_pool().fetchval(
            "SELECT count(*) FROM billing_order WHERE sync_state = $1 AND status = $2",
            SYNC_PENDING,
            ORDER_SUCCESS,
        )
        return int(value or 0)

    # ---- 兑换码 ----

    async def create_redemptions(
        self,
        count: int,
        amount_usd: float,
        name: str = "",
        created_by: str = "",
        expires_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """批量生成兑换码，明文只在这里返回一次。"""
        total = max(1, min(200, int(count)))
        now = datetime.now(timezone.utc)
        created: list[dict[str, Any]] = []
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                for _ in range(total):
                    code = generate_redemption_code()
                    row = await connection.fetchrow(
                        """
                        INSERT INTO billing_redemption (
                            id, code_hash, code_hint, name, amount_usd,
                            status, created_by, expires_at, created_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        RETURNING id
                        """,
                        secrets.token_hex(16),
                        redemption_code_hash(code),
                        code[-4:],
                        str(name or "")[:100],
                        _as_decimal(amount_usd),
                        REDEMPTION_ENABLED,
                        str(created_by or "")[:200],
                        expires_at,
                        now,
                    )
                    created.append({"id": str(row["id"]), "code": code, "amountUsd": float(amount_usd)})
        return created

    async def redeem(self, code: str, user_id: str) -> dict[str, Any]:
        """兑换一张兑换码并立即落账。

        并发安全依赖三层：事务、``FOR UPDATE`` 行锁、以及 ``status`` 上的 CAS。
        兑换成功会写一条 ``channel='redemption'`` 的成功订单，让前端"充值记录"
        与在线支付共用同一份流水口径。
        """
        normalized = str(code or "").strip().upper()
        if not normalized:
            raise BillingStoreError("请输入兑换码")
        code_hash = redemption_code_hash(normalized)
        now = datetime.now(timezone.utc)
        pool = self._require_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    SELECT id, amount_usd, status, expires_at
                    FROM billing_redemption WHERE code_hash = $1 FOR UPDATE
                    """,
                    code_hash,
                )
                if row is None:
                    raise BillingStoreError("兑换码无效")
                status = str(row["status"])
                if status == REDEMPTION_USED:
                    raise BillingStoreError("该兑换码已被使用")
                if status != REDEMPTION_ENABLED:
                    raise BillingStoreError("该兑换码已停用")
                expires_at = row["expires_at"]
                if isinstance(expires_at, datetime) and expires_at <= now:
                    raise BillingStoreError("该兑换码已过期")

                claimed = await connection.execute(
                    """
                    UPDATE billing_redemption
                    SET status = $2, used_by = $3, used_at = $4
                    WHERE id = $1 AND status = $5
                    """,
                    str(row["id"]),
                    REDEMPTION_USED,
                    str(user_id),
                    now,
                    REDEMPTION_ENABLED,
                )
                if not str(claimed).endswith(" 1"):
                    raise BillingStoreError("该兑换码已被使用")

                amount = _as_decimal(row["amount_usd"])
                trade_no = f"RDM{now.strftime('%Y%m%d%H%M%S')}{secrets.token_hex(4).upper()}"
                await connection.execute(
                    """
                    INSERT INTO billing_order (
                        trade_no, user_id, channel, amount_usd, money_cny,
                        exchange_rate, status, payment_method, created_at,
                        completed_at, sync_state
                    )
                    VALUES ($1, $2, $3, $4, 0, 0, $5, '', $6, $6, $7)
                    """,
                    trade_no,
                    str(user_id),
                    CHANNEL_REDEMPTION,
                    amount,
                    ORDER_SUCCESS,
                    now,
                    SYNC_PENDING,
                )
                account_row = await connection.fetchrow(
                    """
                    INSERT INTO billing_account (user_id, balance_usd, topup_total_usd, created_at, updated_at)
                    VALUES ($1, $2, $2, $3, $3)
                    ON CONFLICT (user_id) DO UPDATE
                    SET balance_usd = billing_account.balance_usd + $2,
                        topup_total_usd = billing_account.topup_total_usd + $2,
                        updated_at = $3
                    RETURNING user_id, balance_usd, topup_total_usd, updated_at
                    """,
                    str(user_id),
                    amount,
                    now,
                )
        return {
            "tradeNo": trade_no,
            "amountUsd": _money(amount),
            "account": {
                "userId": str(account_row["user_id"]),
                "balanceUsd": _money(account_row["balance_usd"]),
                "topupTotalUsd": _money(account_row["topup_total_usd"]),
                "updatedAt": _iso(account_row["updated_at"]),
            },
        }

    async def list_redemptions(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        pool = self._require_pool()
        capped = max(1, min(200, int(limit)))
        rows = await pool.fetch(
            """
            SELECT id, code_hint, name, amount_usd, status, created_by,
                   used_by, expires_at, created_at, used_at
            FROM billing_redemption
            ORDER BY created_at DESC LIMIT $1 OFFSET $2
            """,
            capped,
            max(0, int(offset)),
        )
        total = await pool.fetchval("SELECT count(*) FROM billing_redemption")
        return {
            "items": [
                {
                    "id": str(row["id"]),
                    "codeHint": str(row["code_hint"] or ""),
                    "name": str(row["name"] or ""),
                    "amountUsd": _money(row["amount_usd"]),
                    "status": str(row["status"]),
                    "createdBy": str(row["created_by"] or ""),
                    "usedBy": str(row["used_by"] or ""),
                    "expiresAt": _iso(row["expires_at"]),
                    "createdAt": _iso(row["created_at"]),
                    "usedAt": _iso(row["used_at"]),
                }
                for row in rows
            ],
            "total": int(total or 0),
        }

    async def disable_redemption(self, redemption_id: str) -> bool:
        result = await self._require_pool().execute(
            """
            UPDATE billing_redemption SET status = $2
            WHERE id = $1 AND status = $3
            """,
            str(redemption_id),
            REDEMPTION_DISABLED,
            REDEMPTION_ENABLED,
        )
        return str(result).endswith(" 1")

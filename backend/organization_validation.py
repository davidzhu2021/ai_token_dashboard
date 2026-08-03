"""客户企业模块的共享校验与常量。

组织目录有两个实现：进程内演示 store（:mod:`backend.organization_store`）和真实
PostgreSQL 持久化（:mod:`backend.organization_repository`）。两者必须接受完全相同的输入
并抛出完全相同的错误，否则"演示能建的部门真实环境建不了"这类偏差会一路漏到前端。
所以字段校验、金额精度、令牌目录解析全部集中在这里，由两个 store 通过
:class:`OrganizationValidationMixin` 继承，而不是各写一份。

本模块刻意不依赖 HTTP、上游网关和任何 store 实现，因此可以被两边同时导入而不构成
循环依赖。
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


ORGANIZATION_ROLES = frozenset({"admin", "member"})
MEMBER_STATUSES = frozenset({"invited", "active", "suspended"})
ORGANIZATION_STATUSES = frozenset({"active", "suspended", "archived"})
TEAM_ROLES = frozenset({"leader", "member"})
ACTIVE_DEPARTMENT_MEMBER_STATUSES = frozenset({"invited", "active"})

# 演示企业的固定开账额度及模拟充值边界；真实企业使用独立授信账本。
INITIAL_ORGANIZATION_BALANCE_USD = Decimal("5000.00")
MIN_SIMULATED_TOPUP_USD = Decimal("1.00")
MAX_SIMULATED_TOPUP_USD = Decimal("100000.00")

# 可选模型目录的正常来源是网关真实模型列表，由路由层注入（见
# main.organization_token_model_catalog）。这份内置清单只供 demo store 和离线测试；
# real 模式目录不可用时必须拒绝创建 Token。
ORGANIZATION_TOKEN_MODELS = (
    "claude-opus-5",
    "claude-sonnet-4-6",
    "gpt-5.2",
    "qwen3-coder-plus",
    "gemini-3-pro",
)
TOKEN_STATUSES = frozenset({"active", "revoked", "expired"})
TOKEN_DURATIONS = ("never", "30d", "90d")
TOKEN_DURATION_DAYS = {"30d": 30, "90d": 90}
MAX_TOKENS_PER_ORGANIZATION = 20
# 不与内置清单长度挂钩：真实网关目录远多于回落清单，绑死会让请求体在 Pydantic 层
# 就被截断，管理员选不满自己有权使用的模型。
MAX_MODELS_PER_TOKEN = 50
MIN_TOKEN_DAILY_BUDGET_USD = Decimal("1.00")
MAX_TOKEN_DAILY_BUDGET_USD = Decimal("5000.00")
DEFAULT_TOKEN_DAILY_BUDGET_USD = Decimal("100.00")

# 区分"未传该字段"与"显式传 None"，供 update_* 的部分更新语义使用。
_UNSET = object()

_EMAIL_LOCAL_PATTERN = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+\Z")
_EMAIL_DOMAIN_LABEL_PATTERN = re.compile(r"[a-z0-9-]+\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


class OrganizationStoreError(RuntimeError):
    """Base exception raised by organization repository operations."""


class OrganizationValidationError(OrganizationStoreError):
    """Raised when a caller supplies an invalid organization field."""


class OrganizationNotFoundError(OrganizationStoreError):
    """Raised when an organization, department, member, or team does not exist."""


class OrganizationConflictError(OrganizationStoreError):
    """Raised when a requested change violates organization state rules."""


class OrganizationPermissionError(OrganizationStoreError):
    """Raised when a customer member tries to leave their assigned scope."""


class DuplicateMemberEmailError(OrganizationConflictError):
    """Raised when an identity is already assigned to another customer."""


class OrganizationValidationMixin:
    """两个 store 实现共享的输入校验与序列化辅助。

    全部是 ``staticmethod``/``classmethod``，不触碰任何实例状态，因此可以安全地被
    内存实现与 SQLite 实现同时继承。
    """

    @staticmethod
    def normalize_email(email: str) -> str:
        """Normalize and validate an email used for customer membership."""

        if not isinstance(email, str):
            raise OrganizationValidationError("email must be a string")
        value = email.strip().lower()
        if value.count("@") != 1 or len(value) > 254:
            raise OrganizationValidationError("enter a valid email address")
        local, domain = value.split("@", 1)
        if (
            not local
            or len(local) > 64
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
            or not _EMAIL_LOCAL_PATTERN.fullmatch(local)
        ):
            raise OrganizationValidationError("enter a valid email address")
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise OrganizationValidationError("enter a valid email address") from exc
        labels = domain.split(".")
        if (
            len(domain) > 253
            or len(labels) < 2
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or not _EMAIL_DOMAIN_LABEL_PATTERN.fullmatch(label)
                for label in labels
            )
        ):
            raise OrganizationValidationError("enter a valid email address")
        return f"{local}@{domain}"

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    @staticmethod
    def _required_text(value: Any, field_name: str, max_length: int) -> str:
        if not isinstance(value, str):
            raise OrganizationValidationError(f"{field_name} must be a string")
        normalized = " ".join(value.split())
        if not normalized:
            raise OrganizationValidationError(f"{field_name} is required")
        if len(normalized) > max_length:
            raise OrganizationValidationError(f"{field_name} must be at most {max_length} characters")
        if any(ord(character) < 32 for character in normalized):
            raise OrganizationValidationError(f"{field_name} contains invalid characters")
        return normalized

    @staticmethod
    def _required_identifier(value: Any, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise OrganizationValidationError(f"{field_name} is required")
        return value.strip()

    @classmethod
    def _valid_identifier(cls, value: Any, field_name: str) -> str:
        identifier = cls._required_identifier(value, field_name)
        if len(identifier) > 128 or not _IDENTIFIER_PATTERN.fullmatch(identifier):
            raise OrganizationValidationError(f"{field_name} contains invalid characters")
        return identifier

    @staticmethod
    def _normalized_optional_filter(value: Any, field_name: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise OrganizationValidationError(f"{field_name} must be a string")
        return value.strip()

    @classmethod
    def _validate_role(cls, value: Any) -> str:
        if not isinstance(value, str) or value not in ORGANIZATION_ROLES:
            raise OrganizationValidationError("role must be admin or member")
        return value

    @classmethod
    def _validate_status(cls, value: Any) -> str:
        if not isinstance(value, str) or value not in MEMBER_STATUSES:
            raise OrganizationValidationError("status must be invited, active, or suspended")
        return value

    @classmethod
    def _validate_organization_status(cls, value: Any) -> str:
        if not isinstance(value, str) or value not in ORGANIZATION_STATUSES:
            raise OrganizationValidationError("organization status must be active, suspended, or archived")
        return value

    @classmethod
    def _validate_team_role(cls, value: Any) -> str:
        if not isinstance(value, str) or value not in TEAM_ROLES:
            raise OrganizationValidationError("team_role must be leader or member")
        return value

    @staticmethod
    def _page_value(value: Any, field_name: str, maximum: int) -> int:
        if isinstance(value, bool):
            raise OrganizationValidationError(f"{field_name} must be an integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise OrganizationValidationError(f"{field_name} must be an integer") from exc
        if parsed < 1 or parsed > maximum:
            raise OrganizationValidationError(f"{field_name} must be between 1 and {maximum}")
        return parsed

    @staticmethod
    def _money(value: Decimal) -> float:
        """Return a JSON-friendly monetary value rounded to cents."""

        return float(value.quantize(Decimal("0.01")))

    @classmethod
    def _topup_amount(cls, value: Any) -> Decimal:
        """Require an exact finite USD amount with no sub-cent precision."""

        if isinstance(value, bool):
            raise OrganizationValidationError("amountUsd must be a number")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise OrganizationValidationError("amountUsd must be a number") from exc
        if not amount.is_finite():
            raise OrganizationValidationError("amountUsd must be a finite number")
        if amount.as_tuple().exponent < -2:
            raise OrganizationValidationError("amountUsd must have at most two decimal places")
        if amount < MIN_SIMULATED_TOPUP_USD or amount > MAX_SIMULATED_TOPUP_USD:
            raise OrganizationValidationError("amountUsd must be between 1.00 and 100000.00")
        return amount.quantize(Decimal("0.01"))

    @staticmethod
    def _mask_token_value(value: str) -> str:
        """Return the only token representation the store is allowed to keep."""

        return f"sk-...{value[-4:]}"

    @staticmethod
    def _expiry_timestamp(created_at: str, duration: str) -> str:
        days = TOKEN_DURATION_DAYS.get(duration)
        if days is None:
            return ""
        created = datetime.fromisoformat(created_at)
        return (created + timedelta(days=days)).replace(microsecond=0).isoformat()

    @classmethod
    def _token_model_catalog(cls, available_models: Any = None) -> tuple[str, ...]:
        """解析可选模型目录。

        目录由路由层注入（上游网关的真实模型名）。store 本身不发任何请求，所以
        缺省时回落到内置常量，让离线单测与未配置上游的部署仍然可用。
        """
        if available_models is None:
            return ORGANIZATION_TOKEN_MODELS
        if isinstance(available_models, str) or not isinstance(available_models, (list, tuple)):
            raise OrganizationValidationError("available_models must be a list of model names")
        catalog: list[str] = []
        for item in available_models:
            name = item.strip() if isinstance(item, str) else ""
            if name and name not in catalog:
                catalog.append(name)
        return tuple(catalog) or ORGANIZATION_TOKEN_MODELS

    @classmethod
    def _validate_token_models(cls, value: Any, available_models: Any = None) -> tuple[str, ...]:
        catalog = cls._token_model_catalog(available_models)
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise OrganizationValidationError("models must be a list of model names")
        selected: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise OrganizationValidationError("models must be a list of model names")
            name = item.strip()
            if name not in catalog:
                raise OrganizationValidationError("models contains an unavailable model")
            if name not in selected:
                selected.append(name)
        if not selected:
            raise OrganizationValidationError("select at least one model")
        if len(selected) > MAX_MODELS_PER_TOKEN:
            raise OrganizationValidationError(
                f"models must contain at most {MAX_MODELS_PER_TOKEN} entries"
            )
        # Keep catalog order so two identical selections always serialise alike.
        return tuple(name for name in catalog if name in selected)

    @staticmethod
    def _validate_token_duration(value: Any) -> str:
        if not isinstance(value, str) or value not in TOKEN_DURATIONS:
            raise OrganizationValidationError("duration must be never, 30d, or 90d")
        return value

    @staticmethod
    def _validate_token_status(value: Any) -> str:
        if not isinstance(value, str) or value not in TOKEN_STATUSES:
            raise OrganizationValidationError("status must be active, revoked, or expired")
        return value

    @classmethod
    def _token_daily_budget(cls, value: Any) -> Decimal:
        if isinstance(value, bool):
            raise OrganizationValidationError("dailyBudgetUsd must be a number")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise OrganizationValidationError("dailyBudgetUsd must be a number") from exc
        if not amount.is_finite():
            raise OrganizationValidationError("dailyBudgetUsd must be a finite number")
        if amount.as_tuple().exponent < -2:
            raise OrganizationValidationError("dailyBudgetUsd must have at most two decimal places")
        if amount < MIN_TOKEN_DAILY_BUDGET_USD or amount > MAX_TOKEN_DAILY_BUDGET_USD:
            raise OrganizationValidationError("dailyBudgetUsd must be between 1.00 and 5000.00")
        return amount.quantize(Decimal("0.01"))

    @staticmethod
    def _platform_admin_emails() -> set[str]:
        """Read seller-admin addresses without coupling this store to HTTP auth."""

        return {
            value.strip().casefold()
            for value in os.getenv("ADMIN_EMAILS", "").split(",")
            if value.strip()
        }

"""Deterministic in-memory customer-organization data for the product demo.

The store deliberately has no HTTP, authentication, email, or upstream
dependencies.  It models the seller's customer directory and independently
scoped customer organizations, so route handlers can keep platform access
checks separate from a customer's membership role.  A database-backed
implementation can keep the same public operations later.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol


ORGANIZATION_ROLES = frozenset({"admin", "member"})
MEMBER_STATUSES = frozenset({"invited", "active", "suspended"})
ORGANIZATION_STATUSES = frozenset({"active", "suspended", "archived"})
TEAM_ROLES = frozenset({"leader", "member"})
ACTIVE_DEPARTMENT_MEMBER_STATUSES = frozenset({"invited", "active"})

# This is deliberately separate from the production billing ledger.  Mock
# organizations receive a deterministic opening credit that is never reduced
# by generated usage data.
INITIAL_ORGANIZATION_BALANCE_USD = Decimal("5000.00")
MIN_SIMULATED_TOPUP_USD = Decimal("1.00")
MAX_SIMULATED_TOPUP_USD = Decimal("100000.00")

_SEED_TIMESTAMP = "2026-01-01T00:00:00+00:00"
_UNSET = object()
_EMAIL_LOCAL_PATTERN = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+\Z")
_EMAIL_DOMAIN_LABEL_PATTERN = re.compile(r"[a-z0-9-]+\Z")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")
_USAGE_SOURCES = (
    ("Cursor", "gpt-5.2", 12.0),
    ("Claude Code", "claude-sonnet-4-6", 15.0),
    ("其他", "qwen3-coder-plus", 4.0),
)


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
    """Raised when a mock identity is already assigned to another customer."""


class OrganizationStore(Protocol):
    """Application-facing interface for legacy and platform-scoped demo data."""

    def list_organizations(
        self,
        *,
        keyword: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 50,
        include_archived: bool = True,
    ) -> dict[str, Any]: ...

    def get_organization(self, organization_id: str) -> dict[str, Any] | None: ...

    def get_organization_snapshot(self, organization_id: str) -> dict[str, Any]: ...

    def usage_cache_fingerprint(self, organization_id: str) -> str: ...

    def create_organization_with_admin(
        self,
        name: str,
        admin_name: str,
        admin_email: str,
        *,
        default_department_name: str = "企业管理",
        organization_id: str | None = None,
    ) -> dict[str, Any]: ...

    def update_organization(
        self,
        organization_id: str,
        name: Any = _UNSET,
        *,
        status: Any = _UNSET,
    ) -> dict[str, Any]: ...

    def archive_organization(self, organization_id: str) -> dict[str, Any]: ...

    def get_current(self, organization_id: str | None = None) -> dict[str, Any]: ...

    def list_departments(
        self, *, include_archived: bool = False, organization_id: str | None = None
    ) -> list[dict[str, Any]]: ...

    def get_department(
        self, department_id: str, *, organization_id: str | None = None
    ) -> dict[str, Any] | None: ...

    def create_department(
        self, name: str, *, organization_id: str | None = None
    ) -> dict[str, Any]: ...

    def update_department(
        self, department_id: str, name: str, *, organization_id: str | None = None
    ) -> dict[str, Any]: ...

    def archive_department(
        self, department_id: str, *, organization_id: str | None = None
    ) -> dict[str, Any]: ...

    def list_members(
        self,
        *,
        keyword: str = "",
        department_id: str = "",
        role: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 50,
        organization_id: str | None = None,
    ) -> dict[str, Any]: ...

    def get_member(
        self, member_id: str, *, organization_id: str | None = None
    ) -> dict[str, Any] | None: ...

    def get_member_by_email(
        self, email: str, *, organization_id: str | None = None
    ) -> dict[str, Any] | None: ...

    def resolve_member_by_email(self, email: str) -> dict[str, Any] | None: ...

    def create_member(
        self,
        name: str,
        email: str,
        department_id: str,
        role: str = "member",
        *,
        team_role: str = "member",
        organization_id: str | None = None,
    ) -> dict[str, Any]: ...

    def update_member(
        self,
        member_id: str,
        *,
        name: Any = _UNSET,
        department_id: Any = _UNSET,
        role: Any = _UNSET,
        status: Any = _UNSET,
        team_role: Any = _UNSET,
        organization_id: str | None = None,
    ) -> dict[str, Any]: ...

    def billing_payload(
        self,
        organization_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]: ...

    def simulate_billing_topup(
        self,
        organization_id: str,
        amount_usd: Decimal,
        *,
        operator: str,
        operator_email: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]: ...

    def reset(self, organization_id: str | None = None) -> dict[str, Any]: ...


@dataclass
class _Department:
    identifier: str
    name: str
    status: str
    created_at: str
    updated_at: str
    archived_at: str = ""


@dataclass
class _Member:
    identifier: str
    name: str
    email: str
    department_id: str
    role: str
    status: str
    created_at: str
    updated_at: str
    team_role: str = "member"
    # Seeded accounts receive deterministic historical usage. Newly invited
    # members begin with no usage history, even after an operator activates
    # them, until the real data pipeline exists in a later phase.
    has_mock_usage: bool = True


@dataclass
class _BillingRecord:
    identifier: str
    timestamp: str
    record_type: str
    amount_usd: Decimal
    balance_after_usd: Decimal
    operator: str
    operator_email: str
    status: str = "completed"


@dataclass
class _OrganizationBilling:
    initial_balance_usd: Decimal
    total_topups_usd: Decimal
    available_balance_usd: Decimal
    records: list[_BillingRecord]


@dataclass
class _OrganizationState:
    organization: dict[str, Any]
    departments: dict[str, _Department]
    members: dict[str, _Member]
    billing: _OrganizationBilling
    # This private counter changes with directory mutations.  It lets the
    # HTTP layer safely cache generated board data without exposing a mutable
    # organization id or relying on second-resolution timestamps.
    usage_cache_version: int = 1


class InMemoryOrganizationStore:
    """Thread-safe, deterministic platform demo repository.

    ``org-demo`` remains the legacy current tenant so existing routes can keep
    using their original no-organization-id calls during the UI migration.
    Platform callers must pass an explicit organization id (or use
    :meth:`for_organization`) to avoid cross-customer access.
    """

    organization_id = "org-demo"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._organizations: dict[str, _OrganizationState] = {}
        self._usage_cache_namespace = uuid.uuid4().hex
        self._load_seed()

    # ------------------------------------------------------------------
    # Validation and deterministic seed data
    # ------------------------------------------------------------------

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
    def _seed_state(
        organization_id: str,
        name: str,
        departments: tuple[tuple[str, str], ...],
        members: tuple[tuple[str, str, str, str, str, str, str], ...],
    ) -> _OrganizationState:
        billing = InMemoryOrganizationStore._seed_billing()
        return _OrganizationState(
            organization={
                "id": organization_id,
                "name": name,
                "status": "active",
                "isDemo": True,
                "createdAt": _SEED_TIMESTAMP,
                "updatedAt": _SEED_TIMESTAMP,
            },
            departments={
                identifier: _Department(identifier, department_name, "active", _SEED_TIMESTAMP, _SEED_TIMESTAMP)
                for identifier, department_name in departments
            },
            members={
                identifier: _Member(
                    identifier,
                    member_name,
                    email,
                    department_id,
                    role,
                    status,
                    _SEED_TIMESTAMP,
                    _SEED_TIMESTAMP,
                    team_role,
                    True,
                )
                for identifier, member_name, email, department_id, role, status, team_role in members
            },
            billing=billing,
        )

    @staticmethod
    def _seed_billing() -> _OrganizationBilling:
        """Return a fresh deterministic opening balance for one customer."""

        return _OrganizationBilling(
            initial_balance_usd=INITIAL_ORGANIZATION_BALANCE_USD,
            total_topups_usd=Decimal("0.00"),
            available_balance_usd=INITIAL_ORGANIZATION_BALANCE_USD,
            records=[
                _BillingRecord(
                    identifier="billing-initial-credit",
                    timestamp=_SEED_TIMESTAMP,
                    record_type="initial_credit",
                    amount_usd=INITIAL_ORGANIZATION_BALANCE_USD,
                    balance_after_usd=INITIAL_ORGANIZATION_BALANCE_USD,
                    operator="System",
                    operator_email="",
                )
            ],
        )

    def _seed_organizations(self) -> dict[str, _OrganizationState]:
        """Return the fixed platform demo dataset without sharing mutable state."""

        demo = self._seed_state(
            "org-demo",
            "Demo Company",
            (
                ("dept-engineering", "Engineering"),
                ("dept-product", "Product"),
                ("dept-operations", "Operations"),
            ),
            (
                ("member-admin-primary", "Demo Admin", "owner@demo.example", "dept-engineering", "admin", "active", "leader"),
                ("member-admin", "Demo Admin", "admin@demo.example", "dept-product", "admin", "active", "leader"),
                ("member-001", "Avery Chen", "avery.chen@demo.example", "dept-engineering", "member", "active", "member"),
                ("member-002", "Blake Kim", "blake.kim@demo.example", "dept-product", "member", "active", "member"),
                ("member-003", "Casey Lin", "casey.lin@demo.example", "dept-operations", "member", "active", "leader"),
                ("member-004", "Devon Wu", "devon.wu@demo.example", "dept-engineering", "member", "active", "member"),
                ("member-005", "Emery Zhou", "emery.zhou@demo.example", "dept-product", "member", "active", "member"),
                ("member-006", "Flynn Gao", "flynn.gao@demo.example", "dept-engineering", "member", "invited", "member"),
                ("member-007", "Gray Sun", "gray.sun@demo.example", "dept-product", "admin", "invited", "leader"),
                ("member-008", "Harper He", "harper.he@demo.example", "dept-operations", "member", "invited", "member"),
                ("member-009", "Indigo Xu", "indigo.xu@demo.example", "dept-operations", "member", "suspended", "member"),
                ("member-010", "Jules Qian", "jules.qian@demo.example", "dept-engineering", "admin", "suspended", "leader"),
            ),
        )
        aurora = self._seed_state(
            "org-aurora",
            "北辰智造有限公司",
            (
                ("dept-aurora-research", "智能研发部"),
                ("dept-aurora-supply", "供应链中心"),
                ("dept-aurora-service", "客户成功部"),
            ),
            (
                ("member-aurora-admin-primary", "沈宁", "ning.shen@aurora.example", "dept-aurora-research", "admin", "active", "leader"),
                ("member-aurora-admin", "吴迪", "di.wu@aurora.example", "dept-aurora-supply", "admin", "active", "leader"),
                ("member-aurora-001", "林舟", "zhou.lin@aurora.example", "dept-aurora-research", "member", "active", "member"),
                ("member-aurora-002", "周苒", "ran.zhou@aurora.example", "dept-aurora-research", "member", "active", "member"),
                ("member-aurora-003", "陈逸", "yi.chen@aurora.example", "dept-aurora-supply", "member", "active", "member"),
                ("member-aurora-004", "高原", "yuan.gao@aurora.example", "dept-aurora-service", "member", "active", "leader"),
                ("member-aurora-005", "李澄", "cheng.li@aurora.example", "dept-aurora-service", "member", "invited", "member"),
                ("member-aurora-006", "方齐", "qi.fang@aurora.example", "dept-aurora-supply", "member", "suspended", "member"),
            ),
        )
        harbor = self._seed_state(
            "org-harbor",
            "远海零售集团",
            (
                ("dept-harbor-digital", "数字化中心"),
                ("dept-harbor-growth", "增长业务部"),
                ("dept-harbor-stores", "门店运营部"),
            ),
            (
                ("member-harbor-admin-primary", "许岚", "lan.xu@harbor.example", "dept-harbor-digital", "admin", "active", "leader"),
                ("member-harbor-admin", "陆川", "chuan.lu@harbor.example", "dept-harbor-growth", "admin", "active", "leader"),
                ("member-harbor-001", "唐悦", "yue.tang@harbor.example", "dept-harbor-digital", "member", "active", "member"),
                ("member-harbor-002", "苏晴", "qing.su@harbor.example", "dept-harbor-growth", "member", "active", "member"),
                ("member-harbor-003", "罗晨", "chen.luo@harbor.example", "dept-harbor-stores", "member", "active", "leader"),
                ("member-harbor-004", "韩雪", "xue.han@harbor.example", "dept-harbor-stores", "member", "active", "member"),
                ("member-harbor-005", "朱颜", "yan.zhu@harbor.example", "dept-harbor-growth", "member", "invited", "member"),
                ("member-harbor-006", "白宁", "ning.bai@harbor.example", "dept-harbor-digital", "admin", "suspended", "leader"),
            ),
        )
        return {demo.organization["id"]: demo, aurora.organization["id"]: aurora, harbor.organization["id"]: harbor}

    def _load_seed(self) -> None:
        """Replace all state with fixed, side-effect-free customer demo data."""

        self._organizations = self._seed_organizations()
        # A platform reset must not reuse a generated-board cache entry from
        # the previous in-memory dataset, even when both use the same seed id.
        self._usage_cache_namespace = uuid.uuid4().hex

    # ------------------------------------------------------------------
    # State lookup and serialisation helpers
    # ------------------------------------------------------------------

    def _organization_or_raise(self, organization_id: Any) -> _OrganizationState:
        identifier = self._required_identifier(organization_id, "organization_id")
        state = self._organizations.get(identifier)
        if state is None:
            raise OrganizationNotFoundError("organization was not found")
        return state

    def usage_cache_fingerprint(self, organization_id: str) -> str:
        """Return an internal revision for one customer's generated boards."""

        with self._lock:
            state = self._organization_or_raise(organization_id)
            return (
                f"{self._usage_cache_namespace}:"
                f"{state.organization.get('id', '')}:{state.usage_cache_version}"
            )

    @staticmethod
    def _touch_usage_scope(state: _OrganizationState, timestamp: str) -> None:
        """Advance the board revision whenever its visible organization changes."""

        state.usage_cache_version += 1
        state.organization["updatedAt"] = timestamp

    def _state_for(self, organization_id: str | None) -> _OrganizationState:
        return self._organization_or_raise(organization_id or self.organization_id)

    @staticmethod
    def _department_or_raise(state: _OrganizationState, department_id: Any) -> _Department:
        identifier = InMemoryOrganizationStore._required_identifier(department_id, "department_id")
        department = state.departments.get(identifier)
        if department is None:
            raise OrganizationNotFoundError("department was not found")
        return department

    @staticmethod
    def _member_or_raise(state: _OrganizationState, member_id: Any) -> _Member:
        identifier = InMemoryOrganizationStore._required_identifier(member_id, "member_id")
        member = state.members.get(identifier)
        if member is None:
            raise OrganizationNotFoundError("member was not found")
        return member

    def _active_department_or_raise(self, state: _OrganizationState, department_id: Any) -> _Department:
        department = self._department_or_raise(state, department_id)
        if department.status != "active":
            raise OrganizationConflictError("members cannot be assigned to an archived department")
        return department

    @staticmethod
    def _department_key(department: _Department) -> str:
        normalized_name = " ".join(department.name.split()).casefold()
        return f"{department.identifier}::{normalized_name}"

    @staticmethod
    def _team_ref(organization_id: str, department_id: str) -> str:
        return f"mock-{organization_id}-{department_id}"

    def _department_payload(self, state: _OrganizationState, department: _Department) -> dict[str, Any]:
        members = [member for member in state.members.values() if member.department_id == department.identifier]
        return {
            "id": department.identifier,
            "name": department.name,
            "status": department.status,
            "memberCount": len(members),
            "activeMemberCount": sum(member.status == "active" for member in members),
            "invitedMemberCount": sum(member.status == "invited" for member in members),
            "suspendedMemberCount": sum(member.status == "suspended" for member in members),
            "createdAt": department.created_at,
            "updatedAt": department.updated_at,
            "archivedAt": department.archived_at or None,
        }

    def _member_payload(self, state: _OrganizationState, member: _Member) -> dict[str, Any]:
        department = state.departments.get(member.department_id)
        return {
            "id": member.identifier,
            "name": member.name,
            "email": member.email,
            "departmentId": member.department_id,
            "departmentName": department.name if department is not None else "",
            "departmentStatus": department.status if department is not None else "archived",
            "role": member.role,
            "status": member.status,
            "teamRole": member.team_role,
            "isTeamLeader": member.team_role == "leader",
            "createdAt": member.created_at,
            "updatedAt": member.updated_at,
        }

    def _stats_payload(self, state: _OrganizationState) -> dict[str, Any]:
        departments = [item for item in state.departments.values() if item.status == "active"]
        members = list(state.members.values())
        return {
            "departmentCount": len(departments),
            "memberCount": len(members),
            "activeMemberCount": sum(member.status == "active" for member in members),
            "invitedMemberCount": sum(member.status == "invited" for member in members),
            "suspendedMemberCount": sum(member.status == "suspended" for member in members),
            "activeAdminCount": sum(member.status == "active" and member.role == "admin" for member in members),
        }

    @staticmethod
    def _organization_payload(state: _OrganizationState) -> dict[str, Any]:
        return dict(state.organization)

    def _organization_summary_payload(self, state: _OrganizationState) -> dict[str, Any]:
        return {**self._organization_payload(state), "stats": self._stats_payload(state)}

    def _organization_snapshot(self, state: _OrganizationState) -> dict[str, Any]:
        departments = [
            self._department_payload(state, department)
            for department in state.departments.values()
            if department.status == "active"
        ]
        return {
            "organization": self._organization_payload(state),
            "departments": departments,
            "stats": self._stats_payload(state),
        }

    @staticmethod
    def _money(value: Decimal) -> float:
        """Return a JSON-friendly monetary value rounded to cents."""

        return float(value.quantize(Decimal("0.01")))

    @classmethod
    def _billing_record_payload(cls, record: _BillingRecord) -> dict[str, Any]:
        return {
            "id": record.identifier,
            "timestamp": record.timestamp,
            "type": record.record_type,
            "amountUsd": cls._money(record.amount_usd),
            "balanceAfterUsd": cls._money(record.balance_after_usd),
            "operator": record.operator,
            "operatorEmail": record.operator_email,
            "status": record.status,
        }

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

    def _billing_usage_summary(self, state: _OrganizationState) -> dict[str, Any]:
        """Aggregate deterministic usage for display without touching balance."""

        end_date = date.today()
        periods = {
            "today": 1,
            "last7Days": 7,
            "last30Days": 30,
        }
        summary: dict[str, dict[str, Any]] = {}
        for key, days in periods.items():
            start_date = end_date - timedelta(days=days - 1)
            rows = self._usage_rows_for_state(
                state,
                [start_date + timedelta(days=offset) for offset in range(days)],
                "all",
            )
            summary[key] = {
                "spend": round(sum(float(row.get("spend") or 0.0) for row in rows), 6),
                "tokens": sum(int(row.get("totalTokens") or 0) for row in rows),
                "requests": sum(int(row.get("requestCount") or 0) for row in rows),
            }
            # Preserve the field names used by existing usage widgets while
            # giving the billing page compact aliases for its cards.
            summary[key]["totalTokens"] = summary[key]["tokens"]
            summary[key]["requestCount"] = summary[key]["requests"]
        return summary

    def _billing_payload(
        self,
        state: _OrganizationState,
        *,
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        current_page = self._page_value(page, "page", 100000)
        current_page_size = self._page_value(page_size, "page_size", 100)
        billing = state.billing
        records = sorted(billing.records, key=lambda item: (item.timestamp, item.identifier), reverse=True)
        start = (current_page - 1) * current_page_size
        return {
            "organization": self._organization_payload(state),
            "account": {
                "initialBalanceUsd": self._money(billing.initial_balance_usd),
                "totalTopupsUsd": self._money(billing.total_topups_usd),
                "totalCreditsUsd": self._money(billing.initial_balance_usd + billing.total_topups_usd),
                "availableBalanceUsd": self._money(billing.available_balance_usd),
            },
            "usageSummary": self._billing_usage_summary(state),
            "records": {
                "items": [self._billing_record_payload(item) for item in records[start : start + current_page_size]],
                "total": len(records),
                "page": current_page,
                "pageSize": current_page_size,
            },
            "isDemo": True,
            "usageDoesNotAffectBalance": True,
        }

    @staticmethod
    def _has_duplicate_department_name(
        state: _OrganizationState, name: str, exclude_id: str = ""
    ) -> bool:
        normalized = name.casefold()
        return any(
            department.identifier != exclude_id
            and department.status == "active"
            and department.name.casefold() == normalized
            for department in state.departments.values()
        )

    def _has_duplicate_organization_name(self, name: str, exclude_id: str = "") -> bool:
        normalized = name.casefold()
        return any(
            identifier != exclude_id
            and state.organization.get("status") != "archived"
            and str(state.organization.get("name") or "").casefold() == normalized
            for identifier, state in self._organizations.items()
        )

    def _member_email_assignment(self, normalized_email: str) -> tuple[str, _OrganizationState, _Member] | None:
        """Return the only customer assignment for a mock identity, if any."""

        for organization_id, state in self._organizations.items():
            for member in state.members.values():
                if member.email == normalized_email:
                    return organization_id, state, member
        return None

    @staticmethod
    def _platform_admin_emails() -> set[str]:
        """Read seller-admin addresses without coupling this store to HTTP auth."""

        return {
            value.strip().casefold()
            for value in os.getenv("ADMIN_EMAILS", "").split(",")
            if value.strip()
        }

    @staticmethod
    def _ensure_privileged_member_remains(
        state: _OrganizationState, member: _Member, role: str, status: str
    ) -> None:
        """Keep at least one active enterprise administrator per customer."""

        if member.status != "active":
            return
        active_admins = sum(
            item.status == "active" and item.role == "admin" for item in state.members.values()
        )
        leaves_admin_role = member.role == "admin" and (role != "admin" or status != "active")
        if leaves_admin_role and active_admins <= 1:
            raise OrganizationConflictError("at least one active enterprise administrator must remain")

    # ------------------------------------------------------------------
    # Platform customer directory
    # ------------------------------------------------------------------

    def list_organizations(
        self,
        *,
        keyword: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 50,
        include_archived: bool = True,
    ) -> dict[str, Any]:
        needle = self._normalized_optional_filter(keyword, "keyword").casefold()
        target_status = self._normalized_optional_filter(status, "status")
        if target_status:
            self._validate_organization_status(target_status)
        current_page = self._page_value(page, "page", 100000)
        current_page_size = self._page_value(page_size, "page_size", 100)
        with self._lock:
            records = list(self._organizations.values())
            if not include_archived and not target_status:
                records = [state for state in records if state.organization.get("status") != "archived"]
            if needle:
                records = [
                    state
                    for state in records
                    if needle in str(state.organization.get("name") or "").casefold()
                    or needle in str(state.organization.get("id") or "").casefold()
                ]
            if target_status:
                records = [state for state in records if state.organization.get("status") == target_status]
            records.sort(key=lambda state: (str(state.organization.get("name") or "").casefold(), str(state.organization.get("id") or "")))
            total = len(records)
            start = (current_page - 1) * current_page_size
            return {
                "items": [self._organization_summary_payload(state) for state in records[start : start + current_page_size]],
                "total": total,
                "page": current_page,
                "pageSize": current_page_size,
            }

    def get_organization(self, organization_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                return self._organization_summary_payload(self._organization_or_raise(organization_id))
            except (OrganizationNotFoundError, OrganizationValidationError):
                return None

    def get_organization_snapshot(self, organization_id: str) -> dict[str, Any]:
        with self._lock:
            return self._organization_snapshot(self._organization_or_raise(organization_id))

    def create_organization_with_admin(
        self,
        name: str,
        admin_name: str,
        admin_email: str,
        *,
        default_department_name: str = "企业管理",
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically create a customer, its default department, and administrator."""

        normalized_name = self._required_text(name, "organization name", 120)
        normalized_admin_name = self._required_text(admin_name, "member name", 120)
        normalized_admin_email = self.normalize_email(admin_email)
        normalized_department_name = self._required_text(
            default_department_name, "department name", 80
        )
        identifier = (
            self._valid_identifier(organization_id, "organization_id")
            if organization_id is not None
            else f"org-{uuid.uuid4().hex}"
        )
        with self._lock:
            if normalized_admin_email in self._platform_admin_emails():
                raise OrganizationConflictError("a platform administrator cannot be a customer administrator")
            if identifier in self._organizations:
                raise OrganizationConflictError("an organization with this id already exists")
            if self._has_duplicate_organization_name(normalized_name):
                raise OrganizationConflictError("an active organization with this name already exists")
            if self._member_email_assignment(normalized_admin_email) is not None:
                raise DuplicateMemberEmailError(
                    "a member with this email already exists in a customer organization"
                )
            now = self._now()
            department_id = "dept-enterprise-management"
            department = _Department(
                department_id,
                normalized_department_name,
                "active",
                now,
                now,
            )
            admin = _Member(
                f"member-{identifier}-{uuid.uuid4().hex}",
                normalized_admin_name,
                normalized_admin_email,
                department_id,
                "admin",
                "active",
                now,
                now,
                "leader",
                False,
            )
            state = _OrganizationState(
                organization={
                    "id": identifier,
                    "name": normalized_name,
                    "status": "active",
                    "isDemo": True,
                    "createdAt": now,
                    "updatedAt": now,
                    "archivedAt": None,
                },
                departments={department_id: department},
                members={admin.identifier: admin},
                billing=self._seed_billing(),
            )
            self._organizations[identifier] = state
            return {
                "organization": self._organization_summary_payload(state),
                "department": self._department_payload(state, department),
                "admin": self._member_payload(state, admin),
                **self._organization_snapshot(state),
            }

    def update_organization(
        self,
        organization_id: str,
        name: Any = _UNSET,
        *,
        status: Any = _UNSET,
    ) -> dict[str, Any]:
        if name is _UNSET and status is _UNSET:
            raise OrganizationValidationError("at least one organization field is required")
        normalized_name = _UNSET if name is _UNSET else self._required_text(name, "organization name", 120)
        normalized_status = _UNSET if status is _UNSET else self._validate_organization_status(status)
        with self._lock:
            state = self._organization_or_raise(organization_id)
            current_status = str(state.organization.get("status") or "active")
            if current_status == "archived":
                # Archived customers remain visible to the seller for history,
                # but their demo record is intentionally read-only.
                raise OrganizationConflictError("an archived organization cannot be changed in the demo")
            proposed_name = str(state.organization.get("name") or "") if normalized_name is _UNSET else normalized_name
            proposed_status = current_status if normalized_status is _UNSET else normalized_status
            if proposed_name != state.organization.get("name") and self._has_duplicate_organization_name(
                proposed_name, exclude_id=str(state.organization.get("id") or "")
            ):
                raise OrganizationConflictError("an active organization with this name already exists")
            if proposed_name != state.organization.get("name") or proposed_status != current_status:
                now = self._now()
                state.organization["name"] = proposed_name
                state.organization["status"] = proposed_status
                if proposed_status == "archived":
                    state.organization["archivedAt"] = now
                elif "archivedAt" in state.organization:
                    state.organization["archivedAt"] = None
                self._touch_usage_scope(state, now)
            return self._organization_summary_payload(state)

    def archive_organization(self, organization_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._organization_or_raise(organization_id)
            if state.organization.get("status") == "archived":
                return self._organization_summary_payload(state)
            now = self._now()
            state.organization["status"] = "archived"
            state.organization["archivedAt"] = now
            self._touch_usage_scope(state, now)
            return self._organization_summary_payload(state)

    # ------------------------------------------------------------------
    # Mock organization billing
    # ------------------------------------------------------------------

    def billing_payload(
        self,
        organization_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Return one tenant's presentation-only organization balance.

        This method intentionally has no dependency on ``BillingStore``, a
        payment provider, mail, or an upstream client.  Seller routes may read
        archived customer history; customer authorization is enforced by the
        HTTP layer before this method is called.
        """

        with self._lock:
            return self._billing_payload(
                self._organization_or_raise(organization_id),
                page=page,
                page_size=page_size,
            )

    def simulate_billing_topup(
        self,
        organization_id: str,
        amount_usd: Decimal,
        *,
        operator: str,
        operator_email: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Immediately credit a customer-local, non-payment demo balance."""

        amount = self._topup_amount(amount_usd)
        normalized_operator = self._required_text(operator, "operator", 120)
        normalized_email = self.normalize_email(operator_email)
        with self._lock:
            state = self._organization_or_raise(organization_id)
            if state.organization.get("status") != "active":
                raise OrganizationConflictError("billing cannot be changed for an inactive organization")
            billing = state.billing
            billing.total_topups_usd += amount
            billing.available_balance_usd += amount
            record = _BillingRecord(
                identifier=f"billing-topup-{uuid.uuid4().hex}",
                timestamp=self._now(),
                record_type="simulated_topup",
                amount_usd=amount,
                balance_after_usd=billing.available_balance_usd,
                operator=normalized_operator,
                operator_email=normalized_email,
            )
            billing.records.append(record)
            self._touch_usage_scope(state, record.timestamp)
            return {
                "record": self._billing_record_payload(record),
                **self._billing_payload(state, page=page, page_size=page_size),
            }

    def for_organization(self, organization_id: str) -> "OrganizationScope":
        """Return a small tenant facade for route handlers with a selected customer."""

        with self._lock:
            self._organization_or_raise(organization_id)
        return OrganizationScope(self, organization_id)

    # ------------------------------------------------------------------
    # Department operations (organization_id defaults to legacy org-demo)
    # ------------------------------------------------------------------

    def get_current(self, organization_id: str | None = None) -> dict[str, Any]:
        """Return a selected customer's snapshot; no id preserves legacy behavior."""

        with self._lock:
            return self._organization_snapshot(self._state_for(organization_id))

    def list_departments(
        self, *, include_archived: bool = False, organization_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            state = self._state_for(organization_id)
            return [
                self._department_payload(state, department)
                for department in state.departments.values()
                if include_archived or department.status == "active"
            ]

    def get_department(
        self, department_id: str, *, organization_id: str | None = None
    ) -> dict[str, Any] | None:
        with self._lock:
            try:
                state = self._state_for(organization_id)
                department = self._department_or_raise(state, department_id)
            except (OrganizationNotFoundError, OrganizationValidationError):
                return None
            return self._department_payload(state, department)

    def create_department(
        self, name: str, *, organization_id: str | None = None
    ) -> dict[str, Any]:
        normalized = self._required_text(name, "department name", 80)
        with self._lock:
            state = self._state_for(organization_id)
            if state.organization.get("status") != "active":
                raise OrganizationConflictError("departments cannot be changed for an inactive organization")
            if self._has_duplicate_department_name(state, normalized):
                raise OrganizationConflictError("a department with this name already exists")
            now = self._now()
            organization_identifier = str(state.organization.get("id") or "org")
            department = _Department(
                f"dept-{organization_identifier}-{uuid.uuid4().hex}", normalized, "active", now, now
            )
            state.departments[department.identifier] = department
            self._touch_usage_scope(state, now)
            return self._department_payload(state, department)

    def update_department(
        self, department_id: str, name: str, *, organization_id: str | None = None
    ) -> dict[str, Any]:
        normalized = self._required_text(name, "department name", 80)
        with self._lock:
            state = self._state_for(organization_id)
            if state.organization.get("status") != "active":
                raise OrganizationConflictError("departments cannot be changed for an inactive organization")
            department = self._department_or_raise(state, department_id)
            if department.status != "active":
                raise OrganizationConflictError("an archived department cannot be renamed")
            if self._has_duplicate_department_name(state, normalized, exclude_id=department.identifier):
                raise OrganizationConflictError("a department with this name already exists")
            if department.name != normalized:
                now = self._now()
                department.name = normalized
                department.updated_at = now
                self._touch_usage_scope(state, now)
            return self._department_payload(state, department)

    def rename_department(
        self, department_id: str, name: str, *, organization_id: str | None = None
    ) -> dict[str, Any]:
        """Compatibility spelling for callers that describe the PATCH action."""

        return self.update_department(department_id, name, organization_id=organization_id)

    def archive_department(
        self, department_id: str, *, organization_id: str | None = None
    ) -> dict[str, Any]:
        with self._lock:
            state = self._state_for(organization_id)
            if state.organization.get("status") != "active":
                raise OrganizationConflictError("departments cannot be changed for an inactive organization")
            department = self._department_or_raise(state, department_id)
            if department.status == "archived":
                return self._department_payload(state, department)
            has_live_member = any(
                member.department_id == department.identifier
                and member.status in ACTIVE_DEPARTMENT_MEMBER_STATUSES
                for member in state.members.values()
            )
            if has_live_member:
                raise OrganizationConflictError(
                    "move or suspend invited and active members before archiving a department"
                )
            now = self._now()
            department.status = "archived"
            department.archived_at = now
            department.updated_at = now
            self._touch_usage_scope(state, now)
            return self._department_payload(state, department)

    # ------------------------------------------------------------------
    # Member operations (organization_id defaults to legacy org-demo)
    # ------------------------------------------------------------------

    def list_members(
        self,
        *,
        keyword: str = "",
        department_id: str = "",
        role: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 50,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        needle = self._normalized_optional_filter(keyword, "keyword").casefold()
        target_department_id = self._normalized_optional_filter(department_id, "department_id")
        target_role = self._normalized_optional_filter(role, "role")
        target_status = self._normalized_optional_filter(status, "status")
        if target_role:
            self._validate_role(target_role)
        if target_status:
            self._validate_status(target_status)
        current_page = self._page_value(page, "page", 100000)
        current_page_size = self._page_value(page_size, "page_size", 100)
        with self._lock:
            state = self._state_for(organization_id)
            if target_department_id and target_department_id not in state.departments:
                raise OrganizationNotFoundError("department was not found")
            members = list(state.members.values())
            if needle:
                members = [
                    member
                    for member in members
                    if needle in member.name.casefold() or needle in member.email.casefold()
                ]
            if target_department_id:
                members = [member for member in members if member.department_id == target_department_id]
            if target_role:
                members = [member for member in members if member.role == target_role]
            if target_status:
                members = [member for member in members if member.status == target_status]
            total = len(members)
            start = (current_page - 1) * current_page_size
            return {
                "items": [self._member_payload(state, member) for member in members[start : start + current_page_size]],
                "total": total,
                "page": current_page,
                "pageSize": current_page_size,
            }

    def get_member(
        self, member_id: str, *, organization_id: str | None = None
    ) -> dict[str, Any] | None:
        with self._lock:
            try:
                state = self._state_for(organization_id)
                member = self._member_or_raise(state, member_id)
            except (OrganizationNotFoundError, OrganizationValidationError):
                return None
            return self._member_payload(state, member)

    def get_member_by_email(
        self, email: str, *, organization_id: str | None = None
    ) -> dict[str, Any] | None:
        """Find an email within one selected customer; omitted id means org-demo."""

        try:
            normalized = self.normalize_email(email)
        except OrganizationValidationError:
            return None
        with self._lock:
            state = self._state_for(organization_id)
            for member in state.members.values():
                if member.email == normalized:
                    return self._member_payload(state, member)
        return None

    def resolve_member_by_email(self, email: str) -> dict[str, Any] | None:
        """Resolve the one allowed customer membership for an SSO identity.

        Demo identities are globally unique across customer organizations.  The
        returned object preserves both scopes instead of inventing a platform
        role: ``organizationId``, ``organization``, and ``member``.
        """

        try:
            normalized = self.normalize_email(email)
        except OrganizationValidationError:
            return None
        with self._lock:
            match = self._member_email_assignment(normalized)
            if match is None:
                return None
            organization_id, state, member = match
            return {
                "organizationId": organization_id,
                "organization_id": organization_id,
                "organization": self._organization_payload(state),
                "member": self._member_payload(state, member),
            }

    def resolve_members_by_email(self, email: str) -> list[dict[str, Any]]:
        """Compatibility-friendly plural resolver; the result has at most one item."""

        match = self.resolve_member_by_email(email)
        return [match] if match is not None else []

    def create_member(
        self,
        name: str,
        email: str,
        department_id: str,
        role: str = "member",
        *,
        team_role: str = "member",
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_name = self._required_text(name, "member name", 120)
        normalized_email = self.normalize_email(email)
        normalized_role = self._validate_role(role)
        normalized_team_role = self._validate_team_role(team_role)
        with self._lock:
            # Keep platform and customer identity namespaces disjoint. This is
            # also checked when a first customer administrator is created above.
            if normalized_email in self._platform_admin_emails():
                raise OrganizationConflictError("a platform administrator cannot be a customer member")
            state = self._state_for(organization_id)
            if state.organization.get("status") != "active":
                raise OrganizationConflictError("members cannot be changed for an inactive organization")
            department = self._active_department_or_raise(state, department_id)
            existing = self._member_email_assignment(normalized_email)
            if existing is not None:
                raise DuplicateMemberEmailError("a member with this email already exists in a customer organization")
            now = self._now()
            organization_identifier = str(state.organization.get("id") or "org")
            member = _Member(
                f"member-{organization_identifier}-{uuid.uuid4().hex}",
                normalized_name,
                normalized_email,
                department.identifier,
                normalized_role,
                "invited",
                now,
                now,
                normalized_team_role,
                False,
            )
            state.members[member.identifier] = member
            self._touch_usage_scope(state, now)
            return self._member_payload(state, member)

    def update_member(
        self,
        member_id: str,
        *,
        name: Any = _UNSET,
        department_id: Any = _UNSET,
        role: Any = _UNSET,
        status: Any = _UNSET,
        team_role: Any = _UNSET,
        organization_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            name is _UNSET
            and department_id is _UNSET
            and role is _UNSET
            and status is _UNSET
            and team_role is _UNSET
        ):
            raise OrganizationValidationError("at least one member field is required")
        normalized_name = _UNSET if name is _UNSET else self._required_text(name, "member name", 120)
        normalized_role = _UNSET if role is _UNSET else self._validate_role(role)
        normalized_status = _UNSET if status is _UNSET else self._validate_status(status)
        normalized_team_role = _UNSET if team_role is _UNSET else self._validate_team_role(team_role)
        normalized_department_id = (
            _UNSET if department_id is _UNSET else self._required_identifier(department_id, "department_id")
        )
        with self._lock:
            state = self._state_for(organization_id)
            if state.organization.get("status") != "active":
                raise OrganizationConflictError("members cannot be changed for an inactive organization")
            member = self._member_or_raise(state, member_id)
            proposed_role = member.role if normalized_role is _UNSET else normalized_role
            proposed_status = member.status if normalized_status is _UNSET else normalized_status
            proposed_team_role = member.team_role if normalized_team_role is _UNSET else normalized_team_role
            proposed_department_id = (
                member.department_id if normalized_department_id is _UNSET else normalized_department_id
            )
            if proposed_department_id != member.department_id:
                self._active_department_or_raise(state, proposed_department_id)
            elif proposed_status in ACTIVE_DEPARTMENT_MEMBER_STATUSES:
                self._active_department_or_raise(state, proposed_department_id)
            self._ensure_privileged_member_remains(state, member, proposed_role, proposed_status)
            has_change = (
                (normalized_name is not _UNSET and member.name != normalized_name)
                or member.department_id != proposed_department_id
                or member.role != proposed_role
                or member.status != proposed_status
                or member.team_role != proposed_team_role
            )
            if has_change:
                now = self._now()
                if normalized_name is not _UNSET:
                    member.name = normalized_name
                member.department_id = proposed_department_id
                member.role = proposed_role
                member.status = proposed_status
                member.team_role = proposed_team_role
                member.updated_at = now
                self._touch_usage_scope(state, now)
            return self._member_payload(state, member)

    # ------------------------------------------------------------------
    # Deterministic tenant-scoped usage and team adapters
    # ------------------------------------------------------------------

    @staticmethod
    def _usage_days(start_date: str, end_date: str) -> list[date]:
        try:
            start = date.fromisoformat(str(start_date))
            end = date.fromisoformat(str(end_date))
        except (TypeError, ValueError) as exc:
            raise OrganizationValidationError("usage dates must use YYYY-MM-DD") from exc
        if end < start:
            raise OrganizationValidationError("end_date must not be before start_date")
        if (end - start).days > 366:
            raise OrganizationValidationError("usage date range must be at most 366 days")
        return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]

    @staticmethod
    def _stable_number(*parts: str, lower: int, upper: int) -> int:
        if upper < lower:
            raise ValueError("upper must not be less than lower")
        digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
        return lower + int.from_bytes(digest[:8], "big") % (upper - lower + 1)

    @staticmethod
    def _source_selection(source: Any) -> tuple[tuple[str, str, float], ...]:
        value = str(source or "all").strip()
        if not value or value == "all":
            return _USAGE_SOURCES
        return tuple(item for item in _USAGE_SOURCES if item[0] == value)

    @staticmethod
    def _employee_matches(member: _Member, employee: str | None) -> bool:
        needle = str(employee or "").strip().casefold()
        if not needle:
            return True
        return needle in member.identifier.casefold() or needle in member.email.casefold() or needle in member.name.casefold()

    def _usage_rows_for_state(
        self,
        state: _OrganizationState,
        days: list[date],
        source: Any,
        employee: str | None = None,
        department_id: str | None = None,
    ) -> list[dict[str, Any]]:
        organization_id = str(state.organization.get("id") or "")
        selected_sources = self._source_selection(source)
        if not selected_sources:
            return []
        rows: list[dict[str, Any]] = []
        for member in sorted(state.members.values(), key=lambda item: item.identifier):
            if (
                member.status != "active"
                or not member.has_mock_usage
                or not self._employee_matches(member, employee)
            ):
                continue
            if department_id and member.department_id != department_id:
                continue
            department = state.departments.get(member.department_id)
            if department is None or department.status != "active":
                continue
            for usage_day in days:
                day = usage_day.isoformat()
                for source_name, model, price_per_million in selected_sources:
                    seed = (organization_id, member.identifier, department.identifier, day, source_name, model)
                    request_count = self._stable_number(*seed, "requests", lower=3, upper=16)
                    prompt_tokens = request_count * self._stable_number(*seed, "prompt", lower=520, upper=1420)
                    completion_tokens = request_count * self._stable_number(*seed, "completion", lower=180, upper=760)
                    failure_count = 1 if self._stable_number(*seed, "failure", lower=0, upper=18) == 0 else 0
                    success_count = max(0, request_count - failure_count)
                    total_tokens = prompt_tokens + completion_tokens
                    rows.append(
                        {
                            "date": day,
                            "source": source_name,
                            "model": model,
                            "promptTokens": prompt_tokens,
                            "completionTokens": completion_tokens,
                            "totalTokens": total_tokens,
                            "requestCount": request_count,
                            "successCount": success_count,
                            "failureCount": failure_count,
                            "spend": round(total_tokens * price_per_million / 1_000_000, 6),
                            "employeeId": member.identifier,
                            "employeeName": member.name,
                            "employeeEmail": member.email,
                            "bindStatus": "已绑定邮箱",
                            "departmentId": department.identifier,
                            "departmentName": department.name,
                            "departmentKey": self._department_key(department),
                            "departmentBindStatus": "已绑定部门",
                            "organizationId": organization_id,
                        }
                    )
        return sorted(
            rows,
            key=lambda item: (
                str(item["date"]),
                str(item["departmentName"]),
                str(item["employeeName"]),
                str(item["source"]),
                str(item["model"]),
            ),
        )

    def _member_usage_rows_for_state(
        self,
        state: _OrganizationState,
        member: _Member,
        days: list[date],
        source: Any,
    ) -> list[dict[str, Any]]:
        """Return only a member's own Mock history, including an empty start.

        A newly invited member deliberately has no historical data.  Keeping
        that rule here (instead of treating an empty employee filter as a
        match-all filter) prevents a new or suspended customer identity from
        accidentally receiving the whole company's usage response.
        """

        if member.status != "active" or not member.has_mock_usage:
            return []
        return self._usage_rows_for_state(
            state,
            days,
            source,
            employee=member.identifier,
        )

    @staticmethod
    def _empty_metrics() -> dict[str, Any]:
        return {
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "requestCount": 0,
            "successCount": 0,
            "failureCount": 0,
            "spend": 0.0,
        }

    @classmethod
    def _add_metrics(cls, target: dict[str, Any], row: dict[str, Any]) -> None:
        for field in (
            "promptTokens",
            "completionTokens",
            "totalTokens",
            "requestCount",
            "successCount",
            "failureCount",
        ):
            target[field] += int(row.get(field) or 0)
        target["spend"] += float(row.get("spend") or 0)

    def _summary_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("date") or ""), str(row.get("source") or ""), str(row.get("model") or ""))
            summary = grouped.setdefault(
                key,
                {"date": key[0], "source": key[1], "model": key[2], **self._empty_metrics()},
            )
            self._add_metrics(summary, row)
        return sorted(grouped.values(), key=lambda item: (item["date"], item["source"], item["model"]))

    def _employee_summaries(
        self, state: _OrganizationState, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        source_totals: dict[str, dict[str, int]] = {}
        for row in rows:
            member_id = str(row.get("employeeId") or "")
            member = state.members.get(member_id)
            department = state.departments.get(member.department_id) if member else None
            summary = grouped.setdefault(
                member_id,
                {
                    "employeeId": member_id,
                    "employeeName": row.get("employeeName") or member_id,
                    "employeeEmail": row.get("employeeEmail") or "",
                    "bindStatus": row.get("bindStatus") or "已绑定邮箱",
                    "userIds": [member_id],
                    "departmentNames": [department.name] if department else [],
                    "teamRole": "admin" if member and member.team_role == "leader" else "user",
                    **self._empty_metrics(),
                    "primarySource": "其他",
                },
            )
            self._add_metrics(summary, row)
            bucket = source_totals.setdefault(member_id, {})
            source_name = str(row.get("source") or "其他")
            bucket[source_name] = bucket.get(source_name, 0) + int(row.get("totalTokens") or 0)
        for member_id, summary in grouped.items():
            sources = source_totals.get(member_id, {})
            if sources:
                summary["primarySource"] = sorted(sources.items(), key=lambda item: (-item[1], item[0]))[0][0]
            summary["spend"] = round(float(summary["spend"]), 6)
        return sorted(
            grouped.values(),
            key=lambda item: (-int(item["totalTokens"]), -float(item["spend"]), str(item["employeeName"]).casefold()),
        )

    def _department_summaries(
        self,
        state: _OrganizationState,
        rows: list[dict[str, Any]],
        *,
        selected_department_id: str | None = None,
    ) -> list[dict[str, Any]]:
        active_departments = [
            department
            for department in state.departments.values()
            if department.status == "active" and (not selected_department_id or department.identifier == selected_department_id)
        ]
        grouped: dict[str, dict[str, Any]] = {}
        source_totals: dict[str, dict[str, int]] = {}
        employee_ids: dict[str, set[str]] = {}
        for department in active_departments:
            grouped[department.identifier] = {
                "departmentId": department.identifier,
                "departmentName": department.name,
                "departmentKey": self._department_key(department),
                "bindStatus": "已绑定部门",
                "organizationId": state.organization.get("id"),
                "activeEmployees": 0,
                **self._empty_metrics(),
                "primarySource": "其他",
            }
            source_totals[department.identifier] = {}
            employee_ids[department.identifier] = set()
        for row in rows:
            department_id = str(row.get("departmentId") or "")
            summary = grouped.get(department_id)
            if summary is None:
                continue
            self._add_metrics(summary, row)
            employee_ids[department_id].add(str(row.get("employeeId") or ""))
            source_name = str(row.get("source") or "其他")
            bucket = source_totals[department_id]
            bucket[source_name] = bucket.get(source_name, 0) + int(row.get("totalTokens") or 0)
        for department_id, summary in grouped.items():
            summary["activeEmployees"] = len(employee_ids[department_id] - {""})
            sources = source_totals[department_id]
            if sources:
                summary["primarySource"] = sorted(sources.items(), key=lambda item: (-item[1], item[0]))[0][0]
            summary["spend"] = round(float(summary["spend"]), 6)
        return sorted(
            grouped.values(),
            key=lambda item: (-int(item["totalTokens"]), -float(item["spend"]), str(item["departmentName"]).casefold()),
        )

    def _usage_payload_from_rows(
        self,
        state: _OrganizationState,
        rows: list[dict[str, Any]],
        *,
        start_date: str,
        end_date: str,
        source: Any,
        employee: str | None,
        selected_department_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "organization": self._organization_payload(state),
            "rows": rows,
            "summaryRows": self._summary_rows(rows),
            "employees": self._employee_summaries(state, rows),
            "departments": self._department_summaries(
                state, rows, selected_department_id=selected_department_id
            ),
            "startDate": start_date,
            "endDate": end_date,
            "source": str(source or "all"),
            "employee": str(employee or ""),
            "pageLimit": 0,
            "pageSize": 0,
            "pagesRead": 0,
            "totalPages": 0,
            "totalRecords": len(rows),
            "truncated": False,
            "dataQuality": {
                "summarySource": "deterministic_mock",
                "rankingSource": "deterministic_mock",
                "timezoneOffsetMinutes": -480,
                "organizationScoped": True,
            },
        }

    def usage_payload(
        self,
        organization_id: str,
        start_date: str,
        end_date: str,
        source: str = "all",
        employee: str | None = None,
    ) -> dict[str, Any]:
        """Return an admin-dashboard-shaped, customer-isolated mock payload."""

        days = self._usage_days(start_date, end_date)
        with self._lock:
            state = self._organization_or_raise(organization_id)
            rows = self._usage_rows_for_state(state, days, source, employee)
            return self._usage_payload_from_rows(
                state,
                rows,
                start_date=start_date,
                end_date=end_date,
                source=source,
                employee=employee,
            )

    def _department_from_filter(self, state: _OrganizationState, value: str) -> _Department:
        candidate = self._required_identifier(value, "department")
        direct = state.departments.get(candidate)
        if direct is not None:
            return direct
        matches = [
            department
            for department in state.departments.values()
            if candidate == self._department_key(department)
            or candidate.casefold() == department.name.casefold()
        ]
        if len(matches) != 1:
            raise OrganizationNotFoundError("department was not found")
        return matches[0]

    def department_usage_payload(
        self,
        organization_id: str,
        start_date: str,
        end_date: str,
        source: str = "all",
        department: str | None = None,
    ) -> dict[str, Any]:
        """Return department-dashboard-shaped usage for one customer only."""

        days = self._usage_days(start_date, end_date)
        with self._lock:
            state = self._organization_or_raise(organization_id)
            selected_department = self._department_from_filter(state, department) if department else None
            if selected_department is not None and selected_department.status != "active":
                raise OrganizationConflictError("an archived department has no active usage scope")
            rows = self._usage_rows_for_state(
                state,
                days,
                source,
                department_id=selected_department.identifier if selected_department else None,
            )
            payload = self._usage_payload_from_rows(
                state,
                rows,
                start_date=start_date,
                end_date=end_date,
                source=source,
                employee=None,
                selected_department_id=selected_department.identifier if selected_department else None,
            )
            payload["department"] = selected_department.identifier if selected_department else ""
            return payload

    def member_usage_payload(
        self,
        organization_id: str,
        member: str,
        start_date: str,
        end_date: str,
        source: str = "all",
    ) -> dict[str, Any]:
        """Return an individual member's isolated usage rows for a customer view."""

        days = self._usage_days(start_date, end_date)
        with self._lock:
            state = self._organization_or_raise(organization_id)
            target = state.members.get(str(member).strip())
            if target is None:
                normalized = self.normalize_email(member)
                target = next((item for item in state.members.values() if item.email == normalized), None)
            if target is None:
                raise OrganizationNotFoundError("member was not found")
            rows = self._member_usage_rows_for_state(state, target, days, source)
            payload = self._usage_payload_from_rows(
                state,
                rows,
                start_date=start_date,
                end_date=end_date,
                source=source,
                employee=target.identifier,
                selected_department_id=target.department_id,
            )
            payload["member"] = self._member_payload(state, target)
            return payload

    def _team_payload(self, state: _OrganizationState, department: _Department) -> dict[str, Any]:
        organization_id = str(state.organization.get("id") or "")
        members = [
            member
            for member in state.members.values()
            if member.department_id == department.identifier and member.status == "active"
        ]
        return {
            "id": department.identifier,
            "name": department.name,
            "teamRef": self._team_ref(organization_id, department.identifier),
            "departmentId": department.identifier,
            "organizationId": organization_id,
            "memberCount": len(members),
            "backend": "mock",
        }

    def list_teams(
        self, organization_id: str, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        with self._lock:
            state = self._organization_or_raise(organization_id)
            return [
                self._team_payload(state, department)
                for department in state.departments.values()
                if include_archived or department.status == "active"
            ]

    def _team_from_ref(self, state: _OrganizationState, team_ref: str) -> _Department:
        candidate = self._required_identifier(team_ref, "team_ref")
        organization_id = str(state.organization.get("id") or "")
        for department in state.departments.values():
            if candidate in {
                department.identifier,
                self._team_ref(organization_id, department.identifier),
                self._department_key(department),
            }:
                return department
        raise OrganizationNotFoundError("team was not found")

    def team_scope_for_member(self, organization_id: str, email: str) -> dict[str, Any]:
        """Return a current team's leader scope without granting company-wide access."""

        normalized = self.normalize_email(email)
        with self._lock:
            state = self._organization_or_raise(organization_id)
            member = next((item for item in state.members.values() if item.email == normalized), None)
            if member is None or member.status != "active" or member.team_role != "leader":
                return {"isTeamLeader": False, "teamBoardStatus": "none", "team": None, "leaderTeams": []}
            department = self._active_department_or_raise(state, member.department_id)
            team = self._team_payload(state, department)
            return {
                "isTeamLeader": True,
                "teamBoardStatus": "single",
                "team": team,
                "leaderTeams": [team],
            }

    def team_usage_payload(
        self,
        organization_id: str,
        team_ref: str,
        start_date: str,
        end_date: str,
        source: str = "all",
        employee: str | None = None,
    ) -> dict[str, Any]:
        """Return a selected team's isolated payload using the existing dashboard keys."""

        days = self._usage_days(start_date, end_date)
        with self._lock:
            state = self._organization_or_raise(organization_id)
            department = self._team_from_ref(state, team_ref)
            if department.status != "active":
                raise OrganizationConflictError("an archived team has no active usage scope")
            rows = self._usage_rows_for_state(
                state, days, source, employee=employee, department_id=department.identifier
            )
            payload = self._usage_payload_from_rows(
                state,
                rows,
                start_date=start_date,
                end_date=end_date,
                source=source,
                employee=employee,
                selected_department_id=department.identifier,
            )
            payload["team"] = self._team_payload(state, department)
            return payload

    def team_member_usage_payload(
        self,
        organization_id: str,
        team_ref: str,
        employee: str,
        start_date: str,
        end_date: str,
        source: str = "all",
    ) -> dict[str, Any]:
        """Convenience spelling for a selected employee inside an authorized team."""

        return self.team_usage_payload(
            organization_id,
            team_ref,
            start_date,
            end_date,
            source,
            employee=employee,
        )

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self, organization_id: str | None = None) -> dict[str, Any]:
        """Restore deterministic seed state; no id resets the whole platform demo.

        The no-argument return remains the old ``org-demo`` current snapshot,
        which keeps the existing Mock V1 routes and tests compatible.
        """

        with self._lock:
            if organization_id is None:
                self._load_seed()
                return self.get_current()
            identifier = self._required_identifier(organization_id, "organization_id")
            seeded = self._seed_organizations().get(identifier)
            if seeded is None:
                raise OrganizationNotFoundError("organization has no deterministic seed data")
            self._organizations[identifier] = seeded
            # A single-customer reset replaces the object behind a stable id,
            # so use a new namespace instead of ever reviving an old cache key.
            self._usage_cache_namespace = uuid.uuid4().hex
            return self._organization_snapshot(seeded)

    def reset_all(self) -> dict[str, Any]:
        """Explicit platform spelling for resetting all customer demo data."""

        return self.reset()

    # Mock V2 route adapters.  Keeping these thin aliases makes the public
    # HTTP layer read in terms of customer scopes while the deterministic
    # aggregation implementation remains shared with the existing boards.
    def mock_organization_usage(
        self,
        organization_id: str,
        start_date: str,
        end_date: str,
        source: str = "all",
        employee: str | None = None,
    ) -> dict[str, Any]:
        return self.usage_payload(organization_id, start_date, end_date, source, employee)

    def mock_department_usage(
        self,
        organization_id: str,
        start_date: str,
        end_date: str,
        source: str = "all",
        department: str | None = None,
    ) -> dict[str, Any]:
        return self.department_usage_payload(
            organization_id, start_date, end_date, source, department
        )

    def mock_personal_usage(
        self,
        organization_id: str,
        email: str,
        start_date: str,
        end_date: str,
        source: str = "all",
    ) -> dict[str, Any]:
        return self.member_usage_payload(organization_id, email, start_date, end_date, source)

    def mock_team_scope(self, organization_id: str, email: str) -> dict[str, Any]:
        return self.team_scope_for_member(organization_id, email)

    def mock_team_usage(
        self,
        organization_id: str,
        email: str,
        start_date: str,
        end_date: str,
        source: str = "all",
        team_ref: str | None = None,
        include_member_rankings: bool = True,
    ) -> dict[str, Any]:
        scope = self.team_scope_for_member(organization_id, email)
        if not scope.get("isTeamLeader"):
            raise OrganizationPermissionError("member is not a team leader")
        authorized_ref = str((scope.get("team") or {}).get("teamRef") or "")
        selected_ref = team_ref or authorized_ref
        if selected_ref != authorized_ref:
            raise OrganizationPermissionError("team leader may only view their assigned team")
        return self.team_usage_payload(
            organization_id, selected_ref, start_date, end_date, source
        )

    def mock_team_member_usage(
        self,
        organization_id: str,
        email: str,
        employee: str,
        start_date: str,
        end_date: str,
        source: str = "all",
        team_ref: str | None = None,
    ) -> dict[str, Any]:
        scope = self.team_scope_for_member(organization_id, email)
        if not scope.get("isTeamLeader"):
            raise OrganizationPermissionError("member is not a team leader")
        authorized_ref = str((scope.get("team") or {}).get("teamRef") or "")
        selected_ref = team_ref or authorized_ref
        if selected_ref != authorized_ref:
            raise OrganizationPermissionError("team leader may only view their assigned team")
        return self.team_member_usage_payload(
            organization_id, selected_ref, employee, start_date, end_date, source
        )


class OrganizationScope:
    """A lightweight tenant facade for explicit organization-scoped handlers."""

    def __init__(self, store: InMemoryOrganizationStore, organization_id: str) -> None:
        self._store = store
        self.organization_id = organization_id

    def get_current(self) -> dict[str, Any]:
        return self._store.get_current(self.organization_id)

    def get_organization(self) -> dict[str, Any] | None:
        return self._store.get_organization(self.organization_id)

    def usage_cache_fingerprint(self) -> str:
        return self._store.usage_cache_fingerprint(self.organization_id)

    def list_departments(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        return self._store.list_departments(
            include_archived=include_archived, organization_id=self.organization_id
        )

    def get_department(self, department_id: str) -> dict[str, Any] | None:
        return self._store.get_department(department_id, organization_id=self.organization_id)

    def create_department(self, name: str) -> dict[str, Any]:
        return self._store.create_department(name, organization_id=self.organization_id)

    def update_department(self, department_id: str, name: str) -> dict[str, Any]:
        return self._store.update_department(department_id, name, organization_id=self.organization_id)

    def archive_department(self, department_id: str) -> dict[str, Any]:
        return self._store.archive_department(department_id, organization_id=self.organization_id)

    def list_members(self, **kwargs: Any) -> dict[str, Any]:
        return self._store.list_members(organization_id=self.organization_id, **kwargs)

    def get_member(self, member_id: str) -> dict[str, Any] | None:
        return self._store.get_member(member_id, organization_id=self.organization_id)

    def get_member_by_email(self, email: str) -> dict[str, Any] | None:
        return self._store.get_member_by_email(email, organization_id=self.organization_id)

    def create_member(
        self,
        name: str,
        email: str,
        department_id: str,
        role: str = "member",
        *,
        team_role: str = "member",
    ) -> dict[str, Any]:
        return self._store.create_member(
            name,
            email,
            department_id,
            role,
            team_role=team_role,
            organization_id=self.organization_id,
        )

    def update_member(self, member_id: str, **kwargs: Any) -> dict[str, Any]:
        return self._store.update_member(member_id, organization_id=self.organization_id, **kwargs)

    def usage_payload(
        self,
        start_date: str,
        end_date: str,
        source: str = "all",
        employee: str | None = None,
    ) -> dict[str, Any]:
        return self._store.usage_payload(
            self.organization_id, start_date, end_date, source, employee
        )

    def department_usage_payload(
        self,
        start_date: str,
        end_date: str,
        source: str = "all",
        department: str | None = None,
    ) -> dict[str, Any]:
        return self._store.department_usage_payload(
            self.organization_id, start_date, end_date, source, department
        )

    def list_teams(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        return self._store.list_teams(self.organization_id, include_archived=include_archived)

    def team_scope_for_member(self, email: str) -> dict[str, Any]:
        return self._store.team_scope_for_member(self.organization_id, email)

    def team_usage_payload(
        self,
        team_ref: str,
        start_date: str,
        end_date: str,
        source: str = "all",
        employee: str | None = None,
    ) -> dict[str, Any]:
        return self._store.team_usage_payload(
            self.organization_id, team_ref, start_date, end_date, source, employee
        )

    def organization_snapshot(self) -> dict[str, Any]:
        return self._store.get_organization_snapshot(self.organization_id)

    def billing_payload(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._store.billing_payload(self.organization_id, page=page, page_size=page_size)

    def simulate_billing_topup(
        self,
        amount_usd: Decimal,
        *,
        operator: str,
        operator_email: str,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return self._store.simulate_billing_topup(
            self.organization_id,
            amount_usd,
            operator=operator,
            operator_email=operator_email,
            page=page,
            page_size=page_size,
        )

    def mock_organization_usage(self, **kwargs: Any) -> dict[str, Any]:
        return self._store.mock_organization_usage(self.organization_id, **kwargs)

    def mock_department_usage(self, **kwargs: Any) -> dict[str, Any]:
        return self._store.mock_department_usage(self.organization_id, **kwargs)

    def mock_personal_usage(self, email: str, **kwargs: Any) -> dict[str, Any]:
        return self._store.mock_personal_usage(self.organization_id, email, **kwargs)

    def mock_team_scope(self, email: str) -> dict[str, Any]:
        return self._store.mock_team_scope(self.organization_id, email)

    def mock_team_usage(self, email: str, **kwargs: Any) -> dict[str, Any]:
        return self._store.mock_team_usage(self.organization_id, email, **kwargs)

    def mock_team_member_usage(self, email: str, employee: str, **kwargs: Any) -> dict[str, Any]:
        return self._store.mock_team_member_usage(
            self.organization_id, email, employee, **kwargs
        )

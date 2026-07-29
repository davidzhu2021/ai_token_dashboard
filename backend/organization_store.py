"""In-memory organization repository used by the enterprise demo.

The repository has no HTTP, authentication, email, or upstream dependencies.
Route handlers are responsible for deriving the current organization and
authorization from the authenticated session.  Keeping that boundary small
makes it possible to replace this implementation with a persistent
multi-tenant store without changing the application-facing operations.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


ORGANIZATION_ROLES = frozenset({"owner", "admin", "member"})
MEMBER_STATUSES = frozenset({"invited", "active", "suspended"})
ACTIVE_DEPARTMENT_MEMBER_STATUSES = frozenset({"invited", "active"})

_SEED_TIMESTAMP = "2026-01-01T00:00:00+00:00"
_UNSET = object()
_EMAIL_LOCAL_PATTERN = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+\Z")
_EMAIL_DOMAIN_LABEL_PATTERN = re.compile(r"[a-z0-9-]+\Z")


class OrganizationStoreError(RuntimeError):
    """Base exception raised by organization repository operations."""


class OrganizationValidationError(OrganizationStoreError):
    """Raised when a caller supplies an invalid organization field."""


class OrganizationNotFoundError(OrganizationStoreError):
    """Raised when a requested department or member does not exist."""


class OrganizationConflictError(OrganizationStoreError):
    """Raised when a requested change violates organization state rules."""


class DuplicateMemberEmailError(OrganizationConflictError):
    """Raised when an organization already has the supplied member email."""


class OrganizationStore(Protocol):
    """Application-facing repository contract for a single current tenant."""

    def get_current(self) -> dict[str, Any]: ...

    def list_departments(self, *, include_archived: bool = False) -> list[dict[str, Any]]: ...

    def get_department(self, department_id: str) -> dict[str, Any] | None: ...

    def create_department(self, name: str) -> dict[str, Any]: ...

    def update_department(self, department_id: str, name: str) -> dict[str, Any]: ...

    def archive_department(self, department_id: str) -> dict[str, Any]: ...

    def list_members(
        self,
        *,
        keyword: str = "",
        department_id: str = "",
        role: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]: ...

    def get_member(self, member_id: str) -> dict[str, Any] | None: ...

    def get_member_by_email(self, email: str) -> dict[str, Any] | None: ...

    def create_member(
        self,
        name: str,
        email: str,
        department_id: str,
        role: str = "member",
    ) -> dict[str, Any]: ...

    def update_member(
        self,
        member_id: str,
        *,
        name: Any = _UNSET,
        department_id: Any = _UNSET,
        role: Any = _UNSET,
        status: Any = _UNSET,
    ) -> dict[str, Any]: ...

    def reset(self) -> dict[str, Any]: ...


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


class InMemoryOrganizationStore:
    """Thread-safe, deterministic demo repository for one organization.

    This class intentionally keeps all state in process memory.  It is meant
    for the explicitly enabled product demonstration only, not as a durable
    customer data store.
    """

    organization_id = "org-demo"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._organization: dict[str, Any] = {}
        self._departments: dict[str, _Department] = {}
        self._members: dict[str, _Member] = {}
        self._load_seed()

    @staticmethod
    def normalize_email(email: str) -> str:
        """Normalize and validate an email used for organization membership."""

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
            raise OrganizationValidationError("role must be owner, admin, or member")
        return value

    @classmethod
    def _validate_status(cls, value: Any) -> str:
        if not isinstance(value, str) or value not in MEMBER_STATUSES:
            raise OrganizationValidationError("status must be invited, active, or suspended")
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

    def _load_seed(self) -> None:
        """Replace all state with the fixed, side-effect-free demo dataset."""

        self._organization = {
            "id": self.organization_id,
            "name": "Demo Company",
            "status": "active",
            "isDemo": True,
            "createdAt": _SEED_TIMESTAMP,
            "updatedAt": _SEED_TIMESTAMP,
        }
        self._departments = {
            "dept-engineering": _Department(
                "dept-engineering", "Engineering", "active", _SEED_TIMESTAMP, _SEED_TIMESTAMP
            ),
            "dept-product": _Department(
                "dept-product", "Product", "active", _SEED_TIMESTAMP, _SEED_TIMESTAMP
            ),
            "dept-operations": _Department(
                "dept-operations", "Operations", "active", _SEED_TIMESTAMP, _SEED_TIMESTAMP
            ),
        }
        seed_members = (
            ("member-owner", "Demo Owner", "owner@demo.example", "dept-engineering", "owner", "active"),
            ("member-admin", "Demo Admin", "admin@demo.example", "dept-product", "admin", "active"),
            ("member-001", "Avery Chen", "avery.chen@demo.example", "dept-engineering", "member", "active"),
            ("member-002", "Blake Kim", "blake.kim@demo.example", "dept-product", "member", "active"),
            ("member-003", "Casey Lin", "casey.lin@demo.example", "dept-operations", "member", "active"),
            ("member-004", "Devon Wu", "devon.wu@demo.example", "dept-engineering", "member", "active"),
            ("member-005", "Emery Zhou", "emery.zhou@demo.example", "dept-product", "member", "active"),
            ("member-006", "Flynn Gao", "flynn.gao@demo.example", "dept-engineering", "member", "invited"),
            ("member-007", "Gray Sun", "gray.sun@demo.example", "dept-product", "admin", "invited"),
            ("member-008", "Harper He", "harper.he@demo.example", "dept-operations", "member", "invited"),
            ("member-009", "Indigo Xu", "indigo.xu@demo.example", "dept-operations", "member", "suspended"),
            ("member-010", "Jules Qian", "jules.qian@demo.example", "dept-engineering", "admin", "suspended"),
        )
        self._members = {
            identifier: _Member(
                identifier,
                name,
                email,
                department_id,
                role,
                status,
                _SEED_TIMESTAMP,
                _SEED_TIMESTAMP,
            )
            for identifier, name, email, department_id, role, status in seed_members
        }

    def _department_or_raise(self, department_id: Any) -> _Department:
        identifier = self._required_identifier(department_id, "department_id")
        department = self._departments.get(identifier)
        if department is None:
            raise OrganizationNotFoundError("department was not found")
        return department

    def _member_or_raise(self, member_id: Any) -> _Member:
        identifier = self._required_identifier(member_id, "member_id")
        member = self._members.get(identifier)
        if member is None:
            raise OrganizationNotFoundError("member was not found")
        return member

    def _active_department_or_raise(self, department_id: Any) -> _Department:
        department = self._department_or_raise(department_id)
        if department.status != "active":
            raise OrganizationConflictError("members cannot be assigned to an archived department")
        return department

    def _department_payload(self, department: _Department) -> dict[str, Any]:
        members = [member for member in self._members.values() if member.department_id == department.identifier]
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

    def _member_payload(self, member: _Member) -> dict[str, Any]:
        department = self._departments.get(member.department_id)
        return {
            "id": member.identifier,
            "name": member.name,
            "email": member.email,
            "departmentId": member.department_id,
            "departmentName": department.name if department is not None else "",
            "departmentStatus": department.status if department is not None else "archived",
            "role": member.role,
            "status": member.status,
            "createdAt": member.created_at,
            "updatedAt": member.updated_at,
        }

    def _organization_payload(self) -> dict[str, Any]:
        return dict(self._organization)

    def _has_duplicate_department_name(self, name: str, exclude_id: str = "") -> bool:
        normalized = name.casefold()
        return any(
            department.identifier != exclude_id
            and department.status == "active"
            and department.name.casefold() == normalized
            for department in self._departments.values()
        )

    def _ensure_privileged_member_remains(self, member: _Member, role: str, status: str) -> None:
        """Keep a live owner and at least one live management account."""

        if member.status != "active":
            return
        active_owners = sum(
            item.status == "active" and item.role == "owner" for item in self._members.values()
        )
        active_managers = sum(
            item.status == "active" and item.role in {"owner", "admin"}
            for item in self._members.values()
        )
        leaves_owner_role = member.role == "owner" and (role != "owner" or status != "active")
        leaves_manager_role = member.role in {"owner", "admin"} and (
            role not in {"owner", "admin"} or status != "active"
        )
        if leaves_owner_role and active_owners <= 1:
            raise OrganizationConflictError("at least one active owner must remain")
        if leaves_manager_role and active_managers <= 1:
            raise OrganizationConflictError("at least one active owner or admin must remain")

    def get_current(self) -> dict[str, Any]:
        """Return the public organization snapshot for the demo tenant."""

        with self._lock:
            departments = [
                self._department_payload(department)
                for department in self._departments.values()
                if department.status == "active"
            ]
            return {
                "organization": self._organization_payload(),
                "departments": departments,
                "stats": {
                    "departmentCount": len(departments),
                    "memberCount": len(self._members),
                    "activeMemberCount": sum(member.status == "active" for member in self._members.values()),
                    "invitedMemberCount": sum(member.status == "invited" for member in self._members.values()),
                    "suspendedMemberCount": sum(member.status == "suspended" for member in self._members.values()),
                    "activeAdminCount": sum(
                        member.status == "active" and member.role in {"owner", "admin"}
                        for member in self._members.values()
                    ),
                },
            }

    def list_departments(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._department_payload(department)
                for department in self._departments.values()
                if include_archived or department.status == "active"
            ]

    def get_department(self, department_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                department = self._department_or_raise(department_id)
            except (OrganizationNotFoundError, OrganizationValidationError):
                return None
            return self._department_payload(department)

    def create_department(self, name: str) -> dict[str, Any]:
        normalized = self._required_text(name, "department name", 80)
        with self._lock:
            if self._has_duplicate_department_name(normalized):
                raise OrganizationConflictError("a department with this name already exists")
            now = self._now()
            department = _Department(
                f"dept-{uuid.uuid4().hex}", normalized, "active", now, now
            )
            self._departments[department.identifier] = department
            self._organization["updatedAt"] = now
            return self._department_payload(department)

    def update_department(self, department_id: str, name: str) -> dict[str, Any]:
        normalized = self._required_text(name, "department name", 80)
        with self._lock:
            department = self._department_or_raise(department_id)
            if department.status != "active":
                raise OrganizationConflictError("an archived department cannot be renamed")
            if self._has_duplicate_department_name(normalized, exclude_id=department.identifier):
                raise OrganizationConflictError("a department with this name already exists")
            if department.name != normalized:
                now = self._now()
                department.name = normalized
                department.updated_at = now
                self._organization["updatedAt"] = now
            return self._department_payload(department)

    def rename_department(self, department_id: str, name: str) -> dict[str, Any]:
        """Compatibility spelling for callers that describe the PATCH action."""

        return self.update_department(department_id, name)

    def archive_department(self, department_id: str) -> dict[str, Any]:
        with self._lock:
            department = self._department_or_raise(department_id)
            if department.status == "archived":
                return self._department_payload(department)
            has_live_member = any(
                member.department_id == department.identifier
                and member.status in ACTIVE_DEPARTMENT_MEMBER_STATUSES
                for member in self._members.values()
            )
            if has_live_member:
                raise OrganizationConflictError(
                    "move or suspend invited and active members before archiving a department"
                )
            now = self._now()
            department.status = "archived"
            department.archived_at = now
            department.updated_at = now
            self._organization["updatedAt"] = now
            return self._department_payload(department)

    def list_members(
        self,
        *,
        keyword: str = "",
        department_id: str = "",
        role: str = "",
        status: str = "",
        page: int = 1,
        page_size: int = 50,
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
            if target_department_id and target_department_id not in self._departments:
                raise OrganizationNotFoundError("department was not found")
            members = list(self._members.values())
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
            page_members = members[start : start + current_page_size]
            return {
                "items": [self._member_payload(member) for member in page_members],
                "total": total,
                "page": current_page,
                "pageSize": current_page_size,
            }

    def get_member(self, member_id: str) -> dict[str, Any] | None:
        with self._lock:
            try:
                member = self._member_or_raise(member_id)
            except (OrganizationNotFoundError, OrganizationValidationError):
                return None
            return self._member_payload(member)

    def get_member_by_email(self, email: str) -> dict[str, Any] | None:
        try:
            normalized = self.normalize_email(email)
        except OrganizationValidationError:
            return None
        with self._lock:
            for member in self._members.values():
                if member.email == normalized:
                    return self._member_payload(member)
        return None

    def create_member(
        self,
        name: str,
        email: str,
        department_id: str,
        role: str = "member",
    ) -> dict[str, Any]:
        normalized_name = self._required_text(name, "member name", 120)
        normalized_email = self.normalize_email(email)
        normalized_role = self._validate_role(role)
        with self._lock:
            department = self._active_department_or_raise(department_id)
            if any(member.email == normalized_email for member in self._members.values()):
                raise DuplicateMemberEmailError("a member with this email already exists")
            now = self._now()
            member = _Member(
                f"member-{uuid.uuid4().hex}",
                normalized_name,
                normalized_email,
                department.identifier,
                normalized_role,
                "invited",
                now,
                now,
            )
            self._members[member.identifier] = member
            self._organization["updatedAt"] = now
            return self._member_payload(member)

    def update_member(
        self,
        member_id: str,
        *,
        name: Any = _UNSET,
        department_id: Any = _UNSET,
        role: Any = _UNSET,
        status: Any = _UNSET,
    ) -> dict[str, Any]:
        if name is _UNSET and department_id is _UNSET and role is _UNSET and status is _UNSET:
            raise OrganizationValidationError("at least one member field is required")
        normalized_name = _UNSET if name is _UNSET else self._required_text(name, "member name", 120)
        normalized_role = _UNSET if role is _UNSET else self._validate_role(role)
        normalized_status = _UNSET if status is _UNSET else self._validate_status(status)
        normalized_department_id = (
            _UNSET if department_id is _UNSET else self._required_identifier(department_id, "department_id")
        )
        with self._lock:
            member = self._member_or_raise(member_id)
            proposed_role = member.role if normalized_role is _UNSET else normalized_role
            proposed_status = member.status if normalized_status is _UNSET else normalized_status
            proposed_department_id = (
                member.department_id if normalized_department_id is _UNSET else normalized_department_id
            )
            if proposed_department_id != member.department_id:
                self._active_department_or_raise(proposed_department_id)
            elif proposed_status in ACTIVE_DEPARTMENT_MEMBER_STATUSES:
                self._active_department_or_raise(proposed_department_id)
            self._ensure_privileged_member_remains(member, proposed_role, proposed_status)
            has_change = (
                (normalized_name is not _UNSET and member.name != normalized_name)
                or member.department_id != proposed_department_id
                or member.role != proposed_role
                or member.status != proposed_status
            )
            if has_change:
                now = self._now()
                if normalized_name is not _UNSET:
                    member.name = normalized_name
                member.department_id = proposed_department_id
                member.role = proposed_role
                member.status = proposed_status
                member.updated_at = now
                self._organization["updatedAt"] = now
            return self._member_payload(member)

    def reset(self) -> dict[str, Any]:
        """Reset the demo back to the same deterministic seed dataset."""

        with self._lock:
            self._load_seed()
            return self.get_current()

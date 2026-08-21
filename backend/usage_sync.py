from __future__ import annotations

import asyncio
import argparse
import inspect
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .litellm_client import LiteLLMBackend, LiteLLMClient, tool_account_email_prefix, usage_today
from .usage_store import UsageStore


logger = logging.getLogger("ai-token-dashboard.usage-sync")

_GENERIC_HER_PROFILE_NAMES = {
    "admin",
    "administrator",
    "default",
    "default_user_id",
    "guest",
    "n/a",
    "na",
    "none",
    "null",
    "root",
    "system",
    "test",
    "unknown",
    "user",
}


def _env_int(name: str, default: int) -> int:
    import os

    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    import os

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def team_rename_map() -> dict[str, str]:
    """Parse ``USAGE_TEAM_RENAME_MAP`` into an ``old_team_id -> new_team_id`` dict.

    部门改名后，上游目录里往往旧团队（如 ``AI技术院``）仍保持 active 并与新团队
    （如 ``AI Infra部``）并存，同一成员会同时出现在两个团队里。同步归因回填先用
    这份映射把旧团队折叠到新团队，才能把「唯一归属」判定做对。格式为逗号分隔的
    ``旧team_id=新team_id`` 对。
    """

    mapping: dict[str, str] = {}
    for part in os.getenv("USAGE_TEAM_RENAME_MAP", "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        old_id, new_id = (item.strip() for item in part.split("=", 1))
        if old_id and new_id:
            mapping[old_id] = new_id
    return mapping


def _text(value: Any) -> str:
    return str(value or "").strip()


def _email(value: Any) -> str:
    value = _text(value).lower()
    return value if "@" in value else ""


def resolve_display_identity(
    *,
    user_id: str,
    user_record: dict[str, Any] | None = None,
    log_record: dict[str, Any] | None = None,
    directory: dict[Any, Any] | None = None,
    backend_id: str | None = None,
) -> dict[str, str]:
    """Resolve a stable, non-empty display identity from upstream hints.

    The ordering intentionally mirrors the employee-facing trust hierarchy:
    explicit LiteLLM profile aliases first, then metadata/team/log hints,
    persisted cross-backend directory data, and finally deterministic fallbacks.
    """

    user_id_text = _text(user_id)
    user = user_record if isinstance(user_record, dict) else {}
    log = log_record if isinstance(log_record, dict) else {}

    def metadata(record: dict[str, Any]) -> dict[str, Any]:
        value = record.get("metadata")
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    user_meta = metadata(user)
    log_meta = metadata(log)

    def candidate(value: Any) -> str:
        text = _text(value)
        return text if text and text.casefold() != user_id_text.casefold() else ""

    def first_email(*values: Any) -> str:
        for value in values:
            email = _email(value)
            if email:
                return email
        return ""

    email = first_email(
        user.get("user_email"),
        user.get("sso_user_id"),
        user_meta.get("email"),
        user_meta.get("user_email"),
        log.get("user_email"),
        log_meta.get("email"),
        log_meta.get("user_email"),
    )

    candidates: list[tuple[str, str]] = [
        ("litellm_user_alias", candidate(user.get("user_alias"))),
        ("litellm_metadata_display_name", candidate(user_meta.get("display_name"))),
        ("litellm_metadata_owner_name", candidate(user_meta.get("owner_name"))),
        ("team_member_user_alias", candidate(user.get("userAlias"))),
        ("team_member_name", candidate(user.get("name"))),
        ("spendlog_user_alias", candidate(log.get("user_alias"))),
        ("spendlog_name", candidate(log.get("name"))),
        ("spendlog_metadata_display_name", candidate(log_meta.get("display_name"))),
        ("spendlog_metadata_owner_name", candidate(log_meta.get("owner_name"))),
    ]
    for source, name in candidates:
        if name:
            return {
                "name": name,
                "email": email,
                "nameSource": source,
                "confidence": "high",
            }

    directory = directory or {}
    profile: Any = None
    if backend_id:
        profile = directory.get((backend_id, user_id_text))
    if profile is None:
        profile = directory.get(user_id_text)
    if profile is None and isinstance(directory.get("byUserId"), dict):
        profile = directory["byUserId"].get(user_id_text)
    if isinstance(profile, dict):
        directory_email = first_email(profile.get("email"), profile.get("employee_email"))
        directory_name = candidate(profile.get("name") or profile.get("display_name"))
        if directory_email:
            email = email or directory_email
        if directory_name:
            return {
                "name": directory_name,
                "email": email,
                "nameSource": "identity_directory",
                "confidence": "high",
            }

    if email:
        return {
            "name": email.split("@", 1)[0],
            "email": email,
            "nameSource": "email_prefix",
            "confidence": "medium",
        }
    return {
        "name": user_id_text,
        "email": "",
        "nameSource": "user_id",
        "confidence": "low",
    }


def _team_members(team: dict[str, Any]) -> list[dict[str, Any]]:
    value = team.get("members_with_roles") or team.get("membersWithRoles") or []
    if isinstance(value, str):
        try:
            import json

            value = json.loads(value)
        except ValueError:
            return []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


@dataclass
class BackendSnapshot:
    backend_id: str
    rows: list[dict[str, Any]]
    memberships: list[dict[str, Any]]
    # None means raw event coverage was unavailable and existing event rows
    # must not be replaced by a daily-activity fallback.
    events: list[dict[str, Any]] | None = None
    departments: list[dict[str, Any]] | None = None
    # 本次识别出的账号身份，用于把姓名/邮箱回填到同步窗口之外的历史行。
    identities: list[dict[str, Any]] | None = None
    event_start_date: str | None = None
    event_end_date: str | None = None
    events_complete: bool | None = None
    event_replace_start_date: str | None = None
    event_replace_end_date: str | None = None
    event_window_complete: bool | None = None


def _stability_scan_plan(
    desired_start: date,
    desired_end: date,
    state: dict[str, Any] | None,
) -> tuple[date, date, date, date]:
    """Return scan and resulting contiguous coverage windows."""

    state = state or {}
    current_start = state.get("window_start")
    current_end = state.get("window_end")
    if isinstance(current_start, str):
        current_start = date.fromisoformat(current_start[:10])
    if isinstance(current_end, str):
        current_end = date.fromisoformat(current_end[:10])

    if not isinstance(current_start, date) or not isinstance(current_end, date):
        return desired_end, desired_end, desired_end, desired_end
    if bool(state.get("partial")):
        retry_day = min(desired_end, max(desired_start, current_start))
        return retry_day, retry_day, current_start, current_end
    if current_end < desired_end:
        scan_start = min(desired_end, current_end + timedelta(days=1))
        return scan_start, scan_start, current_start, scan_start
    if current_start > desired_start:
        scan_start = max(desired_start, current_start - timedelta(days=1))
        return scan_start, scan_start, scan_start, current_end
    return desired_end, desired_end, desired_start, desired_end


class UsageSynchronizer:
    def __init__(
        self,
        client: LiteLLMClient,
        store: UsageStore,
        organization_repository: Any | None = None,
    ) -> None:
        self.client = client
        self.store = store
        # The repository is optional so demo-mode/unit-test synchronizers keep
        # their lightweight construction. Real mode supplies the durable token
        # mapping used when SpendLogs omit organization/team identifiers.
        self.organization_repository = organization_repository

    async def _identity_directory(self) -> dict[str, Any]:
        """跨后端的员工身份目录。

        两套上游共用同一份 ``carher-*`` 账号编号，但姓名/邮箱只在其中一侧齐全：
        带 ``source`` 的后端有姓名和部门，主站那份同号账号则完全空白。这里把两边
        的目录合成一份，让任一侧缺失的身份信息都能补齐。
        """

        by_user_id: dict[str, dict[str, str]] = {}
        name_to_email: dict[str, set[str]] = {}
        # 邮箱前缀 -> 身份。工具账号按 `cursor-<邮箱前缀>` / `claude-code-<邮箱前缀>`
        # 建号，同一个人的两个账号里往往只有一个带姓名。
        local_to_emails: dict[str, set[str]] = {}
        local_identity: dict[str, dict[str, str]] = {}
        # 编号后缀 -> 身份。少数工具账号的邮箱前缀和编号后缀并不一致
        # （如 `claude-code-t1v` 的邮箱是 `baiyu@`），只能按后缀配对。
        suffix_to_names: dict[str, set[str]] = {}
        suffix_identity: dict[str, dict[str, str]] = {}
        confirmed_her_user_ids: set[str] = set()

        def remember_local(name: str, email: str, department: str = "") -> None:
            local = email.split("@", 1)[0] if email else ""
            if not local:
                return
            local_to_emails.setdefault(local, set()).add(email)
            current = local_identity.setdefault(local, {"name": "", "email": email, "department": ""})
            if name and not current["name"]:
                current["name"] = name
            if department and not current["department"]:
                current["department"] = department

        def remember_tool_suffix(user_id: str, name: str, email: str, department: str = "") -> None:
            suffix = tool_account_email_prefix(user_id)
            if not suffix or not name or name == user_id:
                return
            suffix_to_names.setdefault(suffix, set()).add(name)
            current = suffix_identity.setdefault(suffix, {"name": name, "email": "", "department": ""})
            if email and not current["email"]:
                current["email"] = email
            if department and not current["department"]:
                current["department"] = department

        for backend in getattr(self.client, "backends", None) or []:
            if getattr(backend, "source", ""):
                loader = getattr(self.client, "her_account_index", None)
                if not callable(loader):
                    continue
                try:
                    index = await loader(backend)
                except Exception:
                    logger.exception("failed to load account directory for backend %s", backend.id)
                    continue
                for user_id, profile in (index.get("profiles") or {}).items():
                    user_id_text = _text(user_id)
                    if not user_id_text:
                        continue
                    profile_name = _text(profile.get("name"))
                    profile_email = _email(profile.get("email"))
                    by_user_id[user_id_text] = {
                        "name": profile_name,
                        "email": profile_email,
                        "department": _text(profile.get("department")),
                        "emailSource": _text(profile.get("emailSource")) or ("upstream" if profile_email else ""),
                    }
                    if (
                        backend.id == "her"
                        and user_id_text.casefold().startswith("carher-")
                        and (
                            profile_email
                            or (
                                profile_name
                                and profile_name.casefold() != user_id_text.casefold()
                                and profile_name.casefold() not in _GENERIC_HER_PROFILE_NAMES
                            )
                        )
                    ):
                        confirmed_her_user_ids.add(user_id_text.casefold())
                    remember_local(
                        profile_name,
                        profile_email,
                        _text(profile.get("department")),
                    )
                    remember_tool_suffix(
                        user_id_text,
                        profile_name,
                        profile_email,
                        _text(profile.get("department")),
                    )
                continue
            try:
                users = await self.client.users(backend)
            except Exception:
                logger.exception("failed to load user directory for backend %s", backend.id)
                continue
            for user in users:
                name = _text(user.get("user_alias"))
                email = _email(user.get("user_email") or user.get("sso_user_id"))
                if name and email:
                    name_to_email.setdefault(name, set()).add(email)
                remember_local(name, email)
                remember_tool_suffix(_text(user.get("user_id")), name, email)
        return {
            "byUserId": by_user_id,
            # 只保留唯一映射，同名对应多个邮箱时不做推断。
            "nameToEmail": {name: next(iter(emails)) for name, emails in name_to_email.items() if len(emails) == 1},
            # 同一前缀落在两个不同邮箱上时（跨域同名）不做归并。
            "byEmailLocal": {
                local: identity
                for local, identity in local_identity.items()
                if len(local_to_emails.get(local) or ()) == 1 and identity.get("name")
            },
            # 同一后缀对应多个姓名时同样不猜；后缀本身就是个有歧义的邮箱前缀
            # （同名跨域）时，沿用 byEmailLocal 的保守判断。
            "byToolSuffix": {
                suffix: identity
                for suffix, identity in suffix_identity.items()
                if len(suffix_to_names.get(suffix) or ()) == 1
                and len(local_to_emails.get(suffix) or ()) <= 1
            },
            "confirmedHerUserIds": confirmed_her_user_ids,
        }

    @staticmethod
    def _reclassify_primary_her_usage(
        backend: LiteLLMBackend,
        rows: list[dict[str, Any]],
        directory: dict[str, Any],
    ) -> int:
        """Classify only explicitly identified Primary ``carher-*`` traffic as Her."""

        if backend.id != "primary" or backend.source:
            return 0
        confirmed_ids = {
            _text(user_id).casefold()
            for user_id in directory.get("confirmedHerUserIds") or set()
            if _text(user_id)
        }
        if not confirmed_ids:
            return 0

        updated = 0
        for row in rows:
            user_id = _text(
                row.get("_userId")
                or row.get("userId")
                or row.get("user_id")
                or row.get("user")
            ).casefold()
            if (
                _text(row.get("source")) == "其他"
                and user_id.startswith("carher-")
                and user_id in confirmed_ids
            ):
                row["source"] = "Her"
                updated += 1
        return updated

    def _apply_identity_directory(
        self,
        backend: LiteLLMBackend,
        user_id: str,
        info: dict[str, Any],
        directory: dict[str, Any],
    ) -> dict[str, Any]:
        """用跨后端目录补齐单个账号的姓名/邮箱，并标记邮箱来源。"""

        name = _text(info.get("name"))
        email = _email(info.get("email"))
        department = _text(info.get("department"))
        email_source = "upstream" if email else ""
        # 两套后端共用一个账号编号命名空间，这份 `byUserId` 就是员工档案，对本侧
        # 账号同样适用：用量表在 collect_backend 里另外合并过一次档案，成员表没有，
        # 只按后端过滤会让团队看板上的本侧账号继续只剩编号。
        profile = (directory.get("byUserId") or {}).get(user_id) or {}
        if profile and (not name or name == user_id or not email):
            name = name if name and name != user_id else _text(profile.get("name")) or name
            if not email:
                email = _email(profile.get("email"))
                email_source = _text(profile.get("emailSource")) if email else ""
            department = department or _text(profile.get("department"))
        if not name or name == user_id or not email:
            # 工具账号编号的后缀就是邮箱前缀，同一个人的 cursor / claude-code
            # 两个账号里通常只有一个带姓名，另一个只剩编号。
            prefix = tool_account_email_prefix(user_id)
            paired = (directory.get("byEmailLocal") or {}).get(prefix) if prefix else None
            if not paired and prefix:
                paired = (directory.get("byToolSuffix") or {}).get(prefix)
            if paired:
                if not name or name == user_id:
                    name = _text(paired.get("name")) or name
                if not email:
                    email = _email(paired.get("email"))
                    email_source = "paired_tool_account" if email else email_source
                department = department or _text(paired.get("department"))
        if not email:
            # 上游没有邮箱时，用另一侧目录的「姓名 -> 邮箱」唯一映射推断，
            # 仅用于展示与归并，不参与 resolve_user 的邮箱匹配。
            inferred = (directory.get("nameToEmail") or {}).get(name)
            if inferred:
                email = inferred
                email_source = "inferred_primary_directory"
        return {
            **info,
            "name": name or user_id,
            "email": email,
            "department": department,
            "emailSource": email_source,
        }

    def _member_identity(
        self,
        backend: LiteLLMBackend,
        user_id: str,
        name: str,
        email: str,
        directory: dict[str, Any] | None,
    ) -> tuple[str, str]:
        """团队成员的姓名/邮箱同样走跨后端目录。

        团队看板读的是成员表而不是用量表，上游的成员清单里 ``user_alias`` 时有时无，
        缺失时看板上就只剩一个账号编号。
        """

        name = _text(name)
        email = _email(email)
        if name and name != user_id and email:
            return name, email
        resolved = self._apply_identity_directory(
            backend, user_id, {"name": name, "email": email}, directory or {}
        )
        return _text(resolved.get("name")) or name, _email(resolved.get("email")) or email

    @staticmethod
    def _department_records(teams: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "departmentId": _text(team.get("team_id") or team.get("id")),
                "departmentName": _text(team.get("team_alias") or team.get("alias") or team.get("name")) or _text(team.get("team_id") or team.get("id")),
                "organizationId": _text(team.get("organization_id") or team.get("organizationId") or team.get("org_id") or team.get("orgId")),
                "status": "blocked" if (
                    team.get("blocked") is True
                    or str(team.get("blocked") or "").strip().lower() in {"1", "true", "yes", "on"}
                ) else "active",
            }
            for team in teams
            if _text(team.get("team_id") or team.get("id"))
        ]

    async def sync_department_directories(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for backend in self.client.backends:
            teams = await self.client.teams(backend, include_details=False)
            departments = self._department_records(teams)
            counts[backend.id] = await self.store.replace_department_directory(
                backend.id, departments
            )
        return {"status": "ok", "backends": counts, "departmentCount": sum(counts.values())}

    async def _token_attribution_map(
        self, backend_id: str
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        repository = self.organization_repository
        loader = getattr(repository, "usage_token_attribution_map", None)
        if not callable(loader):
            return {}
        try:
            records = await loader()
        except Exception as exc:
            logger.exception("failed to load organization token attribution mappings")
            # Real-mode settlement depends on this map to distinguish imported
            # report-only keys from managed traffic. Treating a repository
            # outage as an empty map could make explicit Organization fields
            # billable, so fail this backend snapshot without replacing data.
            raise RuntimeError(
                "organization token attribution mappings are unavailable"
            ) from exc
        index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            mapping_backend = _text(record.get("backendId") or "primary")
            if mapping_backend != backend_id:
                continue
            mode = _text(record.get("mode") or "managed")
            fields = (
                ("key_id", "upstreamKeyId"),
                ("key_hash", "upstreamKeyHash"),
            )
            # Aliases are safe recovery hints for managed dashboard keys, but
            # report-only imports require the canonical upstream SHA-256 hash.
            if mode != "report_only":
                fields += (("key_alias", "upstreamKeyAlias"),)
            # Older spend logs can omit key identifiers. A persisted upstream
            # user mapping is the final safe fallback; ambiguous matches stay
            # quarantined below.
            fields += (("user_id", "userId"),)
            for identifier_kind, field in fields:
                value = _text(record.get(field))
                if value:
                    index.setdefault((identifier_kind, value), []).append(record)
        return index

    @staticmethod
    def _row_event_time(row: dict[str, Any]) -> datetime | None:
        value = _text(
            row.get("eventTime")
            or row.get("event_time")
            or row.get("startTime")
            or row.get("start_time")
        )
        if value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return None

    @staticmethod
    def _row_usage_date(row: dict[str, Any]) -> date | None:
        value = _text(row.get("date") or row.get("usageDate") or row.get("usage_date"))
        try:
            return date.fromisoformat(value[:10]) if value else None
        except ValueError:
            return None

    @staticmethod
    def _within_mapping_window(
        row: dict[str, Any], mapping: dict[str, Any]
    ) -> bool:
        if _text(mapping.get("mode")) != "report_only":
            return True
        event_time = UsageSynchronizer._row_event_time(row)
        try:
            effective_from = datetime.fromisoformat(
                _text(mapping.get("effectiveFrom")).replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if effective_from.tzinfo is None:
            effective_from = effective_from.replace(tzinfo=timezone.utc)
        effective_through_text = _text(mapping.get("effectiveThrough"))
        if event_time is None:
            # Daily activity is less precise than raw logs, but a stable Key
            # or User mapping still proves that this aggregate is report-only.
            # Compare whole dates and keep it non-billable rather than letting
            # the fallback path silently charge imported historical assets.
            usage_date = UsageSynchronizer._row_usage_date(row)
            if usage_date is None or usage_date < effective_from.date():
                return False
            if not effective_through_text:
                return True
            try:
                effective_through = datetime.fromisoformat(
                    effective_through_text.replace("Z", "+00:00")
                )
            except ValueError:
                return False
            if effective_through.tzinfo is None:
                effective_through = effective_through.replace(tzinfo=timezone.utc)
            return usage_date <= effective_through.date()
        if not effective_through_text:
            return effective_from <= event_time
        try:
            effective_through = datetime.fromisoformat(
                effective_through_text.replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if effective_through.tzinfo is None:
            effective_through = effective_through.replace(tzinfo=timezone.utc)
        return effective_from <= event_time <= effective_through

    @staticmethod
    def _apply_token_attribution(
        rows: list[dict[str, Any]],
        mapping_index: dict[tuple[str, str], list[dict[str, Any]]],
    ) -> None:
        """Apply an unambiguous token map without overriding tenant evidence.

        Explicit Organization/Team fields remain authoritative. A matching
        token mapping may still add its reporting/billing policy; a mismatch
        is quarantined as a data-quality conflict instead of being charged or
        attributed to either tenant.
        """

        for row in rows:
            candidates: list[dict[str, Any]] = []
            identifiers = (
                ("key_id", _text(row.get("keyId") or row.get("key_id"))),
                ("key_hash", _text(row.get("keyHash") or row.get("key_hash"))),
                ("key_alias", _text(row.get("keyAlias") or row.get("key_alias"))),
                ("user_id", _text(row.get("_userId") or row.get("userId") or row.get("user_id") or row.get("user"))),
            )
            for identifier_kind, value in identifiers:
                if not value:
                    continue
                lookup_kinds = [identifier_kind]
                # SpendLogs expose the stable SHA-256 token in keyId. Match it
                # against either a managed key id or a report-only key hash,
                # while still keeping the two mapping namespaces distinct.
                if identifier_kind == "key_id" and len(value) == 64:
                    lookup_kinds.append("key_hash")
                matches = [
                    item
                    for kind in lookup_kinds
                    for item in mapping_index.get((kind, value), [])
                ]
                candidates.extend(
                    item
                    for item in matches
                    if UsageSynchronizer._within_mapping_window(row, item)
                )
            unique = {id(item): item for item in candidates}
            if len(unique) != 1:
                # An ambiguous alias/hash must remain unattributed rather than
                # risking cross-tenant leakage. If any candidate is a
                # report-only asset, also fail billing closed.
                if any(
                    _text(item.get("mode")) == "report_only"
                    for item in unique.values()
                ):
                    row["attributionSource"] = "report_only_mapping_ambiguous"
                    row["billingEligible"] = False
                continue
            mapping = next(iter(unique.values()))
            explicit_organization_id = _text(
                row.get("organizationId") or row.get("organization_id")
            )
            explicit_team_id = _text(row.get("teamId") or row.get("team_id"))
            mapped_organization_id = _text(mapping.get("organizationId"))
            mapped_team_id = _text(mapping.get("teamId"))
            tenant_conflict = bool(
                explicit_organization_id
                and mapped_organization_id
                and explicit_organization_id != mapped_organization_id
            ) or bool(
                explicit_team_id
                and mapped_team_id
                and explicit_team_id != mapped_team_id
            )
            if tenant_conflict:
                row["organizationId"] = ""
                row["teamId"] = ""
                row["principalId"] = ""
                row.pop("memberId", None)
                row["attributionSource"] = "tenant_mapping_conflict"
                row["billingEligible"] = False
                continue
            row["organizationId"] = (
                explicit_organization_id or mapped_organization_id
            )
            row["teamId"] = explicit_team_id or mapped_team_id
            if not _text(row.get("keyId") or row.get("key_id")):
                row["keyId"] = _text(mapping.get("upstreamKeyId") or mapping.get("upstreamKeyHash"))
            row["principalId"] = _text(mapping.get("principalId"))
            member_id = _text(mapping.get("memberId"))
            if member_id:
                row["memberId"] = member_id
            row["attributionSource"] = _text(
                mapping.get("attributionSource") or "managed_token"
            )
            row["billingEligible"] = bool(mapping.get("billingEligible", True))

    @staticmethod
    def _latest_team_by_user(
        memberships: list[dict[str, Any]],
    ) -> dict[tuple[str, str], tuple[str, str]]:
        """Map each ``(backend_id, user_id)`` to its single latest team.

        只在成员快照的最新日期唯一归属一个团队（应用重命名映射之后）时返回；同一
        用户最新快照横跨多个团队（例如部门改名后旧团队未清理）时视为有歧义，返回
        空结果让调用方保持未归因，避免把跨部门成员算进错误团队。
        """

        rename = team_rename_map()
        by_user: dict[tuple[str, str], dict[str, Any]] = {}
        for item in memberships:
            backend_id = _text(item.get("backendId") or item.get("backend_id"))
            user_id = _text(item.get("userId") or item.get("user_id"))
            team_id = _text(item.get("teamId") or item.get("team_id"))
            snapshot_date = _text(
                item.get("snapshotDate") or item.get("snapshot_date")
            )[:10]
            if not backend_id or not user_id or not team_id or not snapshot_date:
                continue
            resolved_team_id = rename.get(team_id, team_id)
            team_name = _text(
                item.get("teamName") or item.get("team_name") or resolved_team_id
            )
            state = by_user.setdefault((backend_id, user_id), {})
            current_date = state.get("date") or ""
            if snapshot_date > current_date:
                state["date"] = snapshot_date
                state["teams"] = {resolved_team_id: team_name}
            elif snapshot_date == current_date:
                state["teams"] = {
                    **(state.get("teams") or {}),
                    resolved_team_id: team_name,
                }
        result: dict[tuple[str, str], tuple[str, str]] = {}
        for key, state in by_user.items():
            teams = state.get("teams") or {}
            if len(teams) == 1:
                team_id, team_name = next(iter(teams.items()))
                result[key] = (team_id, team_name)
        return result

    def _backfill_team_from_membership(
        self,
        backend_id: str,
        rows: list[dict[str, Any]],
        memberships: list[dict[str, Any]],
    ) -> int:
        """给没有租户证据的用量行按团队目录回填 team_id。

        个人直接调用（个人 key 未注册组织令牌、上游用户元数据缺 team_id）产生的行
        归因后仍是 ``unattributed`` 且 team_id 为空，导致团队/部门看板看不到这部分
        用量，而「我的用量 / 全员看板」按邮箱可见。这里只对「最新成员快照唯一归属
        一个团队」的用户回填 team_id，仅用于团队/部门看板的归属展示：organization_id
        保持为空、billing_eligible 保持 False，不会自动计费。
        """

        if not rows or not memberships:
            return 0
        team_by_user = self._latest_team_by_user(memberships)
        if not team_by_user:
            return 0
        backfilled = 0
        for row in rows:
            user_id = _text(
                row.get("_userId")
                or row.get("userId")
                or row.get("user_id")
                or row.get("user")
            )
            if not user_id:
                continue
            team = team_by_user.get((backend_id, user_id))
            if team is None:
                continue
            # 已有租户/团队证据的行不回填：客户组织用量不能算进内部团队。
            if _text(
                row.get("organizationId")
                or row.get("organization_id")
                or row.get("orgId")
                or row.get("org_id")
                or row.get("userOrganizationId")
                or row.get("user_organization_id")
            ):
                continue
            if _text(
                row.get("teamId")
                or row.get("team_id")
                or row.get("tokenTeamId")
                or row.get("token_team_id")
                or row.get("userTeamId")
                or row.get("user_team_id")
            ):
                continue
            row["teamId"] = team[0]
            row["attributionSource"] = "team_membership_backfill"
            row["billingEligible"] = False
            backfilled += 1
        if backfilled:
            logger.info(
                "team membership backfill backend=%s rows=%s",
                backend_id,
                backfilled,
            )
        return backfilled

    @staticmethod
    def date_range(days: int, end: date | None = None) -> tuple[str, str]:
        end = end or usage_today()
        start = end - timedelta(days=max(1, days) - 1)
        return start.isoformat(), end.isoformat()

    async def sync(self, start_date: str, end_date: str) -> dict[str, Any]:
        run_id = await self.store.begin_sync_run(start_date, end_date)
        lock = None
        try:
            lock = await self.store.try_acquire_sync_lock()
        except Exception as exc:
            await self.store.finish_sync_run(run_id, "failed", 0, 0, exc.__class__.__name__)
            raise
        if lock is None:
            await self.store.finish_sync_run(run_id, "skipped", 0, 0, "已有同步任务正在运行")
            return {"status": "skipped", "rowCount": 0, "backendCount": 0}

        snapshots: list[BackendSnapshot] = []
        errors: list[str] = []
        try:
            directory = await self._identity_directory()
            states_loader = getattr(self.store, "stability_sync_states", None)
            states = await states_loader() if callable(states_loader) else []
            self._stability_state_map = {
                str(item.get("backend_id")): item for item in states if isinstance(item, dict)
            }
            publish_stability = getattr(self.store, "publish_stability_events", None)
            if _env_bool("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", False) and callable(publish_stability):
                for backend in self.client.backends:
                    try:
                        await self._sync_stability_backend(backend, publish_stability)
                    except Exception:
                        logger.exception("standalone stability sync failed for backend %s", backend.id)
                self._stability_collected_separately = True
            for backend in self.client.backends:
                try:
                    snapshots.append(await self.collect_backend(backend, start_date, end_date, directory))
                except Exception as exc:
                    logger.exception("usage snapshot failed for backend %s", backend.id)
                    errors.append(f"{backend.id}: {exc.__class__.__name__}")

            row_count = 0
            snapshot_revision: str | None = None
            expected_backend_count = len(self.client.backends)
            publish_snapshots = getattr(self.store, "publish_snapshots", None)
            if not errors and len(snapshots) == expected_backend_count and callable(publish_snapshots):
                published = await publish_snapshots(start_date, end_date, snapshots)
                row_count = int(published.get("rowCount") or 0)
                snapshot_revision = _text(published.get("snapshotRevision")) or None
            elif errors:
                logger.warning(
                    "usage snapshot publish skipped because collection was incomplete backends=%s/%s",
                    len(snapshots),
                    expected_backend_count,
                )
            else:
                for snapshot in snapshots:
                    replace_snapshot = self.store.replace_backend_snapshot
                    events = getattr(snapshot, "events", None)
                    departments = getattr(snapshot, "departments", None)
                    supports_events = True
                    supports_departments = False
                    try:
                        signature = inspect.signature(replace_snapshot)
                        supports_events = "events" in signature.parameters or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in signature.parameters.values()
                        )
                        supports_departments = "departments" in signature.parameters or any(
                            parameter.kind is inspect.Parameter.VAR_KEYWORD
                            for parameter in signature.parameters.values()
                        )
                    except (TypeError, ValueError):
                        pass
                    kwargs = {}
                    if supports_events and events is not None:
                        kwargs["events"] = events
                    if supports_departments:
                        kwargs["departments"] = departments
                    row_count += await replace_snapshot(
                        snapshot.backend_id,
                        start_date,
                        end_date,
                        snapshot.rows,
                        snapshot.memberships,
                        **kwargs,
                    )
            status = "partial" if errors and snapshots else "failed" if errors else "ok"
            await self._refresh_historical_identity(snapshots)
            await self.store.finish_sync_run(run_id, status, len(snapshots), row_count, "; ".join(errors))
            return {
                "status": status,
                "rowCount": row_count,
                "backendCount": len(snapshots),
                "errors": errors,
                "snapshotRevision": snapshot_revision,
            }
        except Exception as exc:
            await self.store.finish_sync_run(run_id, "failed", len(snapshots), 0, exc.__class__.__name__)
            raise
        finally:
            if lock is not None:
                await self.store.release_sync_lock(lock)

    async def _refresh_historical_identity(self, snapshots: list[BackendSnapshot]) -> None:
        """把本次识别到的姓名/邮箱回填到同步窗口之外的历史行。

        匹配规则升级后，旧日期的行仍留着当时写入的空姓名。这一步只更新身份列，
        失败不影响本次同步结果。
        """

        refresh = getattr(self.store, "refresh_account_identity", None)
        directory_upsert = getattr(self.store, "upsert_identity_directory", None)
        directory_refresh = getattr(self.store, "refresh_usage_identity_columns", None)
        for snapshot in snapshots:
            identities = getattr(snapshot, "identities", None)
            if not identities:
                continue
            try:
                if callable(directory_upsert):
                    await directory_upsert(snapshot.backend_id, identities)
                if callable(directory_refresh):
                    await directory_refresh([snapshot.backend_id])
                if callable(refresh):
                    await refresh(snapshot.backend_id, identities)
            except Exception:
                logger.exception("usage identity refresh failed for backend %s", snapshot.backend_id)

    async def _sync_stability_backend(self, backend: LiteLLMBackend, publish: Any) -> None:
        stability_days = max(1, _env_int("STABILITY_SYNC_WINDOW_DAYS", 7))
        desired_end = usage_today()
        desired_start = desired_end - timedelta(days=stability_days - 1)
        scan_start, scan_end, merged_start, merged_end = _stability_scan_plan(
            desired_start,
            desired_end,
            getattr(self, "_stability_state_map", {}).get(backend.id),
        )
        rows, complete = await self.client.stability_rows_from_logs(
            scan_start.isoformat(), scan_end.isoformat(), backend
        )
        events = list(getattr(rows, "events", None) or rows.pop("__events__", []))
        await publish(
            backend.id,
            scan_start.isoformat(),
            scan_end.isoformat(),
            events,
            merged_start.isoformat(),
            merged_end.isoformat(),
            complete,
        )
        self._stability_state_map[backend.id] = {
            "backend_id": backend.id,
            "window_start": merged_start,
            "window_end": merged_end,
            "partial": not complete,
        }

    async def collect_backend(
        self,
        backend: LiteLLMBackend,
        start_date: str,
        end_date: str,
        directory: dict[str, Any] | None = None,
    ) -> BackendSnapshot:
        users = await self.client.users(backend)
        user_map = self.client._admin_user_map(users)
        if directory is None:
            directory = await self._identity_directory()
        account_index: dict[str, Any] = {}
        if backend.source == "Her":
            try:
                account_index = await self.client.her_account_index(backend)
            except Exception:
                logger.exception("failed to load account metadata for backend %s", backend.id)
        account_users: dict[str, dict[str, Any]] = {}
        for user in users:
            user_id = _text(user.get("user_id"))
            if not user_id or not self.client._is_backend_usage_account(backend, user_id):
                continue
            info = user_map.get(user_id.lower()) or {
                "id": _email(user.get("user_email") or user.get("sso_user_id")) or user_id,
                "name": _text(user.get("user_alias")) or user_id,
                "email": _email(user.get("user_email") or user.get("sso_user_id")),
                "bindStatus": "已绑定邮箱" if _email(user.get("user_email") or user.get("sso_user_id")) else "未绑定邮箱",
            }
            profile = account_index.get("profiles", {}).get(user_id) or {}
            info = {
                **info,
                "email": _email(info.get("email")) or _email(profile.get("email")),
                "name": _text(info.get("name")) or _text(profile.get("name")) or user_id,
                "department": _text(info.get("department")) or _text(profile.get("department")),
            }
            info = self._apply_identity_directory(backend, user_id, info, directory)
            account_users[user_id] = {**info, "userId": user_id}

        # 优先按北京时间日界扫描原始日志：上游 daily activity 按 UTC 归日，会把
        # 本地 00:00-08:00 的用量算进前一天。日志扫描失败时退回原有逐账号聚合。
        #
        # 扫描单日约需 3 分钟（全局 8 万条日志、每页上限 100 条），因此只对增量同步的
        # 短窗口启用；初始回填这类长窗口仍走 daily activity，避免一次同步跑上数小时。
        log_rows: dict[str, list[dict[str, Any]]] | None = None
        event_rows: list[dict[str, Any]] = []
        event_start_date: str | None = None
        event_end_date: str | None = None
        events_complete: bool | None = None
        event_replace_start_date: str | None = None
        event_replace_end_date: str | None = None
        event_window_complete: bool | None = None
        token_mapping_index = await self._token_attribution_map(backend.id)
        window_days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
        max_window = max(1, _env_int("USAGE_SYNC_LOG_MAX_WINDOW_DAYS", 3))
        if _env_bool("USAGE_SYNC_LOG_TIMEZONE_ENABLED", True) and not backend.source:
            if window_days > max_window:
                logger.info(
                    "usage log scan skipped backend=%s window=%s days exceeds %s; using daily activity",
                    backend.id,
                    window_days,
                    max_window,
                )
            else:
                try:
                    scanned, complete = await self.client.sync_rows_from_logs(start_date, end_date, backend)
                    if complete:
                        log_rows = scanned
                        event_rows = list(
                            getattr(log_rows, "events", None)
                            or log_rows.pop("__events__", [])
                        )
                        event_start_date, event_end_date, events_complete = start_date, end_date, True
                    else:
                        logger.warning(
                            "usage log scan incomplete for backend %s; falling back to daily activity",
                            backend.id,
                        )
                except Exception:
                    logger.exception("usage log scan failed for backend %s; falling back to daily activity", backend.id)

        # Stability requires a bounded recent raw-log window even when the
        # regular 90-day aggregate sync intentionally avoids request scans.
        if _env_bool("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", False) and not getattr(
            self, "_stability_collected_separately", False
        ):
            stability_days = max(1, _env_int("STABILITY_SYNC_WINDOW_DAYS", 7))
            stability_end = min(date.fromisoformat(end_date), usage_today())
            # Keep the dashboard snapshot window independent from the shorter
            # recent usage refresh window, so a 2-day billing refresh cannot
            # silently shrink the 7-day stability coverage.
            desired_start = stability_end - timedelta(days=stability_days - 1)
            stability_start, stability_scan_end, merged_start, merged_end = _stability_scan_plan(
                desired_start,
                stability_end,
                getattr(self, "_stability_state_map", {}).get(backend.id),
            )
            stability_start_text, stability_end_text = stability_start.isoformat(), stability_scan_end.isoformat()
            try:
                stability_rows, stability_complete = await self.client.stability_rows_from_logs(
                    stability_start_text, stability_end_text, backend
                )
                stability_events = list(getattr(stability_rows, "events", None) or stability_rows.pop("__events__", []))
                event_rows = stability_events
                event_replace_start_date, event_replace_end_date = stability_start_text, stability_end_text
                events_complete = stability_complete
                event_start_date = merged_start.isoformat()
                event_end_date = merged_end.isoformat()
                event_window_complete = stability_complete
            except Exception:
                logger.exception("stability log scan failed for backend %s", backend.id)
                # Preserve the last good raw-event snapshot when the upstream
                # scan is unavailable; an empty partial response would erase
                # useful dashboard data.
                if event_start_date is None:
                    event_rows, event_start_date, event_end_date, events_complete = [], None, None, None
                    event_window_complete = None

        semaphore = asyncio.Semaphore(max(1, _env_int("USAGE_SYNC_USER_CONCURRENCY", 4)))

        async def collect_user(user_id: str, info: dict[str, Any]) -> list[dict[str, Any]]:
            if log_rows is not None:
                rows = log_rows.get(user_id, [])
            else:
                async with semaphore:
                    encoder = getattr(self.client, "_encode_account_id", None)
                    routed_user_id = encoder(backend, user_id) if encoder else user_id
                    rows = await self.client.usage_rows(routed_user_id, start_date, end_date, "all")
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                metadata = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
                item.update(
                    {
                        "_userId": user_id,
                        "employeeEmail": _email(info.get("email")),
                        "employeeName": _text(info.get("name")) or user_id,
                        "emailSource": _text(info.get("emailSource")),
                    }
                )
                # Explicit request-log attribution stays authoritative. Only
                # fill missing fields from persisted upstream user metadata.
                if not _text(item.get("organizationId") or item.get("organization_id")):
                    item["userOrganizationId"] = _text(
                        info.get("organization_id")
                        or info.get("organizationId")
                        or metadata.get("organization_id")
                    )
                if not _text(item.get("teamId") or item.get("team_id")):
                    item["userTeamId"] = _text(
                        info.get("team_id") or info.get("teamId") or metadata.get("team_id")
                    )
                result.append(item)
            return result

        results = await asyncio.gather(
            *(collect_user(user_id, info) for user_id, info in account_users.items()),
        )
        rows = [row for batch in results for row in batch]
        # Full scans may contain user ids missing from /user/list. Preserve all
        # buckets so stable principal mappings can still attribute them and
        # unknown identities remain visible to data-quality checks.
        if log_rows is not None:
            known_user_ids = set(account_users)
            for raw_user_id, raw_rows in log_rows.items():
                if raw_user_id in known_user_ids:
                    continue
                # 全量扫描可能带出 /user/list 里没有的账号，跨后端目录与配对的工具账号仍可能认识它。
                if raw_user_id == "unattributed":
                    resolved = {"name": "未归属请求", "email": "", "emailSource": ""}
                else:
                    resolved = self._apply_identity_directory(
                        backend, raw_user_id, {"name": "", "email": ""}, directory
                    )
                fallback_email = _email(resolved.get("email"))
                fallback_name = _text(resolved.get("name")) or raw_user_id
                for row in raw_rows:
                    rows.append(
                        {
                            **row,
                            "_userId": raw_user_id,
                            "employeeEmail": fallback_email,
                            "employeeName": fallback_name,
                            "emailSource": _text(resolved.get("emailSource")) if fallback_email else "",
                        }
                    )
        reclassified_rows = self._reclassify_primary_her_usage(backend, rows, directory)
        reclassified_events = self._reclassify_primary_her_usage(backend, event_rows, directory)
        if token_mapping_index:
            self._apply_token_attribution(rows, token_mapping_index)
            self._apply_token_attribution(event_rows, token_mapping_index)
        logger.info(
            "usage snapshot collected backend=%s users=%s rows=%s start=%s end=%s her_rows=%s her_events=%s",
            backend.id,
            len(account_users),
            len(rows),
            start_date,
            end_date,
            reclassified_rows,
            reclassified_events,
        )
        memberships = await self.collect_memberships(backend, users, start_date, end_date, account_index, directory)
        self._backfill_team_from_membership(backend.id, rows, memberships)
        directory_teams = getattr(self, "_directory_teams", {}).get(backend.id)
        if directory_teams is None:
            try:
                directory_teams = await self.client.teams(backend, include_details=False)
            except TypeError:
                directory_teams = await self.client.teams(backend)
        departments = self._department_records(directory_teams)
        identities = []
        for user_id, info in account_users.items():
            resolved_identity = resolve_display_identity(
                user_id=user_id,
                user_record=info,
                directory=directory,
                backend_id=backend.id,
            )
            resolved_email = resolved_identity["email"] or _email(info.get("email"))
            if not resolved_email:
                profile = (directory.get("byUserId") or {}).get(user_id) or {}
                resolved_email = _email(profile.get("email"))
            identities.append(
                {
                    "userId": user_id,
                    "name": resolved_identity["name"],
                    "email": resolved_email,
                    "nameSource": resolved_identity["nameSource"],
                    "confidence": resolved_identity["confidence"],
                    "emailSource": _text(info.get("emailSource")),
                }
            )
        return BackendSnapshot(
            backend.id,
            rows,
            memberships,
            event_rows if event_start_date is not None else None,
            departments,
            identities,
            event_start_date,
            event_end_date,
            events_complete,
            event_replace_start_date,
            event_replace_end_date,
            event_window_complete,
        )

    async def collect_memberships(
        self,
        backend: LiteLLMBackend,
        users: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        account_index: dict[str, Any] | None = None,
        directory: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        teams = await self.client.teams(backend)
        directory_cache = getattr(self, "_directory_teams", None)
        if directory_cache is None:
            directory_cache = self._directory_teams = {}
        directory_cache[backend.id] = teams
        user_map = self.client._admin_user_map(users)
        account_index = account_index or {}
        account_by_email: dict[str, list[str]] = {}
        seen_user_ids: set[str] = set()
        for info in user_map.values():
            email = _email(info.get("email"))
            if email:
                for item in info.get("userIds") or []:
                    user_id = _text(item)
                    if user_id and user_id not in seen_user_ids:
                        account_by_email.setdefault(email, []).append(user_id)
                        seen_user_ids.add(user_id)
        for user_id, profile in (account_index.get("profiles", {}) if account_index else {}).items():
            normalized_user_id = _text(user_id)
            email = _email(profile.get("email"))
            if normalized_user_id and email and normalized_user_id not in seen_user_ids:
                account_by_email.setdefault(email, []).append(normalized_user_id)
                seen_user_ids.add(normalized_user_id)
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        dates: list[str] = []
        current = start
        while current <= end:
            dates.append(current.isoformat())
            current += timedelta(days=1)

        memberships: list[dict[str, Any]] = []
        assigned_user_ids: set[str] = set()
        for team in teams:
            team_id = _text(team.get("team_id") or team.get("id"))
            if not team_id:
                continue
            team_name = _text(team.get("team_alias") or team.get("alias") or team.get("name")) or team_id
            for member in _team_members(team):
                user_id = _text(member.get("user_id") or member.get("userId"))
                email = _email(member.get("user_email") or member.get("userEmail") or member.get("email"))
                info = user_map.get(user_id.lower()) or user_map.get(email) or {}
                name = _text(member.get("user_alias") or member.get("userAlias") or member.get("name")) or _text(info.get("name")) or user_id or "unknown"
                candidate_ids = [user_id] if user_id else list(account_by_email.get(email, []))
                if not candidate_ids:
                    candidate_ids = [str(item) for item in info.get("userIds") or [] if item]
                if email and hasattr(self.client, "resolve_user"):
                    try:
                        resolved = await self.client.resolve_user(email, name)
                        matched_accounts = resolved.get("matched_accounts") or []
                        candidate_ids.extend(
                            _text(item.get("user_id"))
                            for item in matched_accounts
                            if isinstance(item, dict) and _text(item.get("backend")) == backend.id and _text(item.get("user_id"))
                        )
                    except Exception:
                        logger.debug("failed to expand team member accounts for %s", email, exc_info=True)
                candidate_ids = list(dict.fromkeys(candidate_ids))
                email = email or _email(info.get("email"))
                name = name or (candidate_ids[0] if candidate_ids else "unknown")
                role = _text(member.get("role") or member.get("user_role") or member.get("team_role")) or "user"
                for candidate_user_id in candidate_ids:
                    assigned_user_ids.add(candidate_user_id)
                    member_name, member_email = self._member_identity(
                        backend, candidate_user_id, name, email, directory
                    )
                    for snapshot_date in dates:
                        memberships.append(
                            {
                                "snapshotDate": snapshot_date,
                                "teamId": team_id,
                                "teamName": team_name,
                                "userId": candidate_user_id,
                                "employeeEmail": member_email,
                                "employeeName": member_name,
                                "teamRole": role,
                            }
                        )
        account_user_ids = {
            _text(user.get("user_id"))
            for user in users
            if _text(user.get("user_id")) and self.client._is_backend_usage_account(backend, user.get("user_id"))
        }
        for user_id in sorted(account_user_ids - assigned_user_ids):
            info = user_map.get(user_id.lower(), {})
            member_name, member_email = self._member_identity(
                backend, user_id, _text(info.get("name")), _email(info.get("email")), directory
            )
            for snapshot_date in dates:
                memberships.append(
                    {
                        "snapshotDate": snapshot_date,
                        "teamId": "unassigned",
                        "teamName": "未分配部门",
                        "userId": user_id,
                        "employeeEmail": member_email,
                        "employeeName": member_name or user_id,
                        "teamRole": "user",
                    }
                )
        return memberships


async def run_sync_once(
    client: LiteLLMClient,
    store: UsageStore,
    days: int,
    organization_repository: Any | None = None,
    synchronizer_factory: Any | None = None,
) -> dict[str, Any]:
    start_date, end_date = UsageSynchronizer.date_range(days)
    factory = synchronizer_factory or UsageSynchronizer
    return await factory(client, store, organization_repository).sync(
        start_date, end_date
    )


async def run_sync_with_recent_refresh(
    client: LiteLLMClient,
    store: UsageStore,
    days: int,
    organization_repository: Any | None = None,
    synchronizer_factory: Any | None = None,
) -> dict[str, Any]:
    """Refresh recent request logs after an efficient long aggregate backfill."""

    if organization_repository is None and synchronizer_factory is None:
        result = await run_sync_once(client, store, days)
    else:
        result = await run_sync_once(
            client,
            store,
            days,
            organization_repository,
            synchronizer_factory,
        )
    if result.get("status") != "ok" or not _env_bool("USAGE_SYNC_LOG_TIMEZONE_ENABLED", True):
        return result

    recent_days = min(days, max(1, _env_int("USAGE_SYNC_LOG_MAX_WINDOW_DAYS", 3)))
    if recent_days >= days:
        return result

    # Keep organization attribution enabled on both passes; otherwise the
    # accurate recent replacement can lose imported/managed token ownership.
    if organization_repository is None and synchronizer_factory is None:
        recent_result = await run_sync_once(client, store, recent_days)
    else:
        recent_result = await run_sync_once(
            client,
            store,
            recent_days,
            organization_repository,
            synchronizer_factory,
        )
    output = dict(result)
    output["recentRefresh"] = {"days": recent_days, **recent_result}
    output["snapshotRevision"] = (
        recent_result.get("snapshotRevision") or result.get("snapshotRevision")
    )
    if recent_result.get("status") != "ok":
        output["status"] = "partial"
        output["errors"] = [
            *list(result.get("errors") or []),
            *list(recent_result.get("errors") or []),
        ]
    return output


async def run_usage_backfill_once(
    client: LiteLLMClient,
    store: UsageStore,
    organization_repository: Any,
    *,
    max_windows: int = 1,
) -> dict[str, Any]:
    """Consume bounded key-scoped historical imports without replacing snapshots."""

    completed = 0
    row_count = 0
    for _ in range(max(1, max_windows)):
        job = await organization_repository.claim_usage_backfill_window(
            max_window_days=3
        )
        if job is None:
            break
        try:
            backend = next(
                item for item in client.backends if item.id == job["backendId"]
            )
            grouped, complete = await client.sync_rows_from_logs(
                job["windowFrom"],
                job["windowThrough"],
                backend,
                api_key=job["upstreamKeyHash"],
            )
            if not complete:
                raise RuntimeError("historical key log scan is incomplete")
            rows = [
                row
                for key, batch in grouped.items()
                if key != "__events__"
                for row in batch
            ]
            events = list(getattr(grouped, "events", None) or grouped.pop("__events__", []))
            mapping = {
                "backendId": job["backendId"],
                "upstreamKeyId": job["upstreamKeyId"],
                "upstreamKeyHash": job["upstreamKeyHash"],
                "organizationId": job["upstreamOrganizationId"],
                "teamId": job["upstreamTeamId"],
                "principalId": job["principalId"],
                "mode": "report_only",
                "attributionSource": "legacy_report_only",
                "billingEligible": False,
                "effectiveFrom": job["effectiveFrom"],
                "effectiveThrough": job["effectiveThrough"],
            }
            expected_key_ids = {
                _text(job["upstreamKeyHash"]),
                _text(job.get("upstreamKeyId")),
            }
            expected_key_ids.discard("")
            for row in [*rows, *events]:
                raw_user_id = _text(row.get("_userId") or row.get("userId") or row.get("user_id"))
                expected_user_id = _text(job.get("upstreamUserId"))
                if expected_user_id and raw_user_id and raw_user_id != expected_user_id:
                    raise RuntimeError("historical key logs returned another upstream user")
                returned_key_id = _text(
                    row.get("keyId")
                    or row.get("key_id")
                    or row.get("keyHash")
                    or row.get("key_hash")
                )
                if not returned_key_id or returned_key_id not in expected_key_ids:
                    raise RuntimeError(
                        "historical key log filter was not confirmed by each row"
                    )
            index = {("key_hash", job["upstreamKeyHash"]): [mapping]}
            if job["upstreamKeyId"]:
                index[("key_id", job["upstreamKeyId"])] = [mapping]
            UsageSynchronizer._apply_token_attribution(rows, index)
            UsageSynchronizer._apply_token_attribution(events, index)
            unattributed = [
                row
                for row in [*rows, *events]
                if _text(row.get("principalId")) != job["principalId"]
            ]
            if unattributed:
                raise RuntimeError("historical key logs could not be attributed safely")
            row_count += await store.upsert_attributed_usage(
                job["backendId"], rows, events=events
            )
            await organization_repository.complete_usage_backfill_window(
                job["id"],
                lease_token=job["leaseToken"],
                covered_from=date.fromisoformat(job["windowFrom"]),
                covered_through=date.fromisoformat(job["windowThrough"]),
            )
            completed += 1
        except Exception as exc:
            await organization_repository.fail_usage_backfill_window(
                job["id"], str(exc), lease_token=job["leaseToken"]
            )
            logger.exception("organization usage backfill failed id=%s", job["id"])
            break
    return {"completedWindowCount": completed, "rowCount": row_count}


async def run_pending_usage_backfills(
    client: LiteLLMClient,
    store: UsageStore,
    organization_repository: Any,
    *,
    max_windows: int = 128,
) -> dict[str, Any]:
    """Drain the bounded queue while retaining retry state on the first failure."""

    completed = 0
    row_count = 0
    for _ in range(max(1, max_windows)):
        result = await run_usage_backfill_once(
            client, store, organization_repository, max_windows=1
        )
        if not result["completedWindowCount"]:
            break
        completed += int(result["completedWindowCount"])
        row_count += int(result["rowCount"])
    return {"completedWindowCount": completed, "rowCount": row_count}


async def _run_cli(days: int) -> int:
    store = UsageStore.from_environment()
    if store is None:
        print(json.dumps({"status": "disabled", "error": "USAGE_DATABASE_URL is not configured"}))
        return 2
    client: LiteLLMClient | None = None
    repository: Any | None = None
    try:
        client = LiteLLMClient()
        await store.connect()
        if os.getenv("ORGANIZATION_MODE", "disabled").strip().lower() == "real":
            from .organization_repository import PostgreSQLOrganizationRepository

            repository = PostgreSQLOrganizationRepository.from_environment()
            if repository is not None:
                await repository.connect()
        result = await run_sync_with_recent_refresh(client, store, days, repository)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") == "ok" else 1
    except Exception as exc:
        logger.exception("one-shot usage sync failed")
        print(json.dumps({"status": "failed", "error": exc.__class__.__name__}))
        return 1
    finally:
        if client is not None:
            await client.close()
        if repository is not None:
            await repository.close()
        await store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely rebuild usage snapshots.")
    parser.add_argument("--days", type=int, required=True, help="Inclusive number of days to rebuild.")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")
    return asyncio.run(_run_cli(args.days))


if __name__ == "__main__":
    sys.exit(main())

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

from .litellm_client import LiteLLMBackend, LiteLLMClient, usage_today
from .usage_store import UsageStore


logger = logging.getLogger("ai-token-dashboard.usage-sync")


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


def _text(value: Any) -> str:
    return str(value or "").strip()


def _email(value: Any) -> str:
    value = _text(value).lower()
    return value if "@" in value else ""


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

    async def _token_attribution_map(
        self, backend_id: str
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        repository = self.organization_repository
        loader = getattr(repository, "usage_token_attribution_map", None)
        if not callable(loader):
            return {}
        try:
            records = await loader()
        except Exception:
            logger.exception("failed to load organization token attribution mappings")
            return {}
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
        # A report-only decision must use the request timestamp. A daily
        # aggregate cannot prove which side of an intraday cutoff it belongs
        # to, so fail closed instead of guessing from the date.
        return None

    @staticmethod
    def _within_mapping_window(
        row: dict[str, Any], mapping: dict[str, Any]
    ) -> bool:
        if _text(mapping.get("mode")) != "report_only":
            return True
        event_time = UsageSynchronizer._row_event_time(row)
        if event_time is None:
            return False
        try:
            effective_from = datetime.fromisoformat(
                _text(mapping.get("effectiveFrom")).replace("Z", "+00:00")
            )
        except ValueError:
            return False
        if effective_from.tzinfo is None:
            effective_from = effective_from.replace(tzinfo=timezone.utc)
        effective_through_text = _text(mapping.get("effectiveThrough"))
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
                # risking cross-tenant leakage.
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
            for backend in self.client.backends:
                try:
                    snapshots.append(await self.collect_backend(backend, start_date, end_date))
                except Exception as exc:
                    logger.exception("usage snapshot failed for backend %s", backend.id)
                    errors.append(f"{backend.id}: {exc.__class__.__name__}")

            row_count = 0
            for snapshot in snapshots:
                replace_snapshot = self.store.replace_backend_snapshot
                events = getattr(snapshot, "events", None)
                supports_events = True
                try:
                    signature = inspect.signature(replace_snapshot)
                    supports_events = "events" in signature.parameters or any(
                        parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in signature.parameters.values()
                    )
                except (TypeError, ValueError):
                    pass
                kwargs = {"events": events} if supports_events and events is not None else {}
                row_count += await replace_snapshot(
                    snapshot.backend_id,
                    start_date,
                    end_date,
                    snapshot.rows,
                    snapshot.memberships,
                    **kwargs,
                )
            status = "partial" if errors and snapshots else "failed" if errors else "ok"
            await self.store.finish_sync_run(run_id, status, len(snapshots), row_count, "; ".join(errors))
            return {
                "status": status,
                "rowCount": row_count,
                "backendCount": len(snapshots),
                "errors": errors,
            }
        except Exception as exc:
            await self.store.finish_sync_run(run_id, "failed", len(snapshots), 0, exc.__class__.__name__)
            raise
        finally:
            if lock is not None:
                await self.store.release_sync_lock(lock)

    async def collect_backend(self, backend: LiteLLMBackend, start_date: str, end_date: str) -> BackendSnapshot:
        users = await self.client.users(backend)
        user_map = self.client._admin_user_map(users)
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
            info = {
                **info,
                "email": _email(info.get("email")) or _email((account_index.get("profiles", {}).get(user_id) or {}).get("email")),
                "name": _text(info.get("name")) or _text((account_index.get("profiles", {}).get(user_id) or {}).get("name")) or user_id,
            }
            account_users[user_id] = {**info, "userId": user_id}

        # 优先按北京时间日界扫描原始日志：上游 daily activity 按 UTC 归日，会把
        # 本地 00:00-08:00 的用量算进前一天。日志扫描失败时退回原有逐账号聚合。
        #
        # 扫描单日约需 3 分钟（全局 8 万条日志、每页上限 100 条），因此只对增量同步的
        # 短窗口启用；初始回填这类长窗口仍走 daily activity，避免一次同步跑上数小时。
        log_rows: dict[str, list[dict[str, Any]]] | None = None
        event_rows: list[dict[str, Any]] = []
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
                        if token_mapping_index:
                            for batch in log_rows.values():
                                self._apply_token_attribution(batch, token_mapping_index)
                            self._apply_token_attribution(
                                event_rows, token_mapping_index
                            )
                    else:
                        logger.warning(
                            "usage log scan incomplete for backend %s; falling back to daily activity",
                            backend.id,
                        )
                except Exception:
                    logger.exception("usage log scan failed for backend %s; falling back to daily activity", backend.id)

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
                for row in raw_rows:
                    rows.append(
                        {
                            **row,
                            "_userId": raw_user_id,
                            "employeeEmail": "",
                            "employeeName": (
                                raw_user_id
                                if raw_user_id != "unattributed"
                                else "未归属请求"
                            ),
                        }
                    )
        logger.info(
            "usage snapshot collected backend=%s users=%s rows=%s start=%s end=%s",
            backend.id,
            len(account_users),
            len(rows),
            start_date,
            end_date,
        )
        memberships = await self.collect_memberships(backend, users, start_date, end_date, account_index)
        return BackendSnapshot(
            backend.id,
            rows,
            memberships,
            event_rows if log_rows is not None else None,
        )

    async def collect_memberships(
        self,
        backend: LiteLLMBackend,
        users: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        account_index: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        teams = await self.client.teams(backend)
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
                    for snapshot_date in dates:
                        memberships.append(
                            {
                                "snapshotDate": snapshot_date,
                                "teamId": team_id,
                                "teamName": team_name,
                                "userId": candidate_user_id,
                                "employeeEmail": email,
                                "employeeName": name,
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
            for snapshot_date in dates:
                memberships.append(
                    {
                        "snapshotDate": snapshot_date,
                        "teamId": "unassigned",
                        "teamName": "未分配部门",
                        "userId": user_id,
                        "employeeEmail": _email(info.get("email")),
                        "employeeName": _text(info.get("name")) or user_id,
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
    if recent_result.get("status") != "ok":
        output["status"] = "partial"
        output["errors"] = [
            *list(result.get("errors") or []),
            *list(recent_result.get("errors") or []),
        ]
    return output


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

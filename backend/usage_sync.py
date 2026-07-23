from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, timedelta
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


class UsageSynchronizer:
    def __init__(self, client: LiteLLMClient, store: UsageStore) -> None:
        self.client = client
        self.store = store

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
                row_count += await self.store.replace_backend_snapshot(
                    snapshot.backend_id,
                    start_date,
                    end_date,
                    snapshot.rows,
                    snapshot.memberships,
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

        semaphore = asyncio.Semaphore(max(1, _env_int("USAGE_SYNC_USER_CONCURRENCY", 4)))

        async def collect_user(user_id: str, info: dict[str, Any]) -> list[dict[str, Any]]:
            async with semaphore:
                encoder = getattr(self.client, "_encode_account_id", None)
                routed_user_id = encoder(backend, user_id) if encoder else user_id
                rows = await self.client.usage_rows(routed_user_id, start_date, end_date, "all")
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item.update(
                    {
                        "_userId": user_id,
                        "employeeEmail": _email(info.get("email")),
                        "employeeName": _text(info.get("name")) or user_id,
                    }
                )
                result.append(item)
            return result

        results = await asyncio.gather(
            *(collect_user(user_id, info) for user_id, info in account_users.items()),
        )
        rows = [row for batch in results for row in batch]
        logger.info(
            "usage snapshot collected backend=%s users=%s rows=%s start=%s end=%s",
            backend.id,
            len(account_users),
            len(rows),
            start_date,
            end_date,
        )
        memberships = await self.collect_memberships(backend, users, start_date, end_date, account_index)
        return BackendSnapshot(backend.id, rows, memberships)

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


async def run_sync_once(client: LiteLLMClient, store: UsageStore, days: int) -> dict[str, Any]:
    start_date, end_date = UsageSynchronizer.date_range(days)
    return await UsageSynchronizer(client, store).sync(start_date, end_date)

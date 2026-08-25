from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .litellm_client import LiteLLMBackend, LiteLLMClient, usage_today
from .usage_realtime import UsageRealtimeStore
from .usage_store import UsageStore
from .usage_sync import UsageSynchronizer, resolve_display_identity


logger = logging.getLogger("ai-token-dashboard.usage-realtime-worker")


def new_worker_id() -> str:
    """Return an owner token that cannot be reused by a restarted process."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class UsageRealtimeWorker:
    def __init__(
        self,
        client: LiteLLMClient,
        store: UsageStore,
        realtime: UsageRealtimeStore,
        repository: Any | None = None,
        *,
        worker_id: str,
    ) -> None:
        self.client = client
        self.store = store
        self.realtime = realtime
        self.synchronizer = UsageSynchronizer(client, store, repository)
        self.worker_id = worker_id
        self.stop_event = asyncio.Event()
        self.poll_seconds = max(2, _env_int("USAGE_REALTIME_POLL_SECONDS", 10))
        self.backend_timeout_seconds = max(
            2, _env_int("USAGE_REALTIME_BACKEND_TIMEOUT_SECONDS", 20)
        )
        self.live_cycle_timeout_seconds = max(
            self.backend_timeout_seconds,
            _env_int("USAGE_REALTIME_LIVE_CYCLE_TIMEOUT_SECONDS", 45),
        )
        self.background_budget_seconds = max(
            1, _env_int("USAGE_REALTIME_BACKGROUND_BUDGET_SECONDS", 5)
        )
        self.overlap_seconds = max(
            1, _env_int("USAGE_REALTIME_OVERLAP_SECONDS", 60)
        )
        self.reconcile_seconds = max(
            60, _env_int("USAGE_REALTIME_RECONCILE_INTERVAL_SECONDS", 300)
        )
        self.lock_ttl_seconds = max(
            60,
            int(
                getattr(
                    realtime,
                    "lock_ttl_seconds",
                    _env_int("USAGE_REALTIME_LOCK_TTL_SECONDS", 300),
                )
            ),
        )
        self.lock_renew_seconds = max(
            1,
            min(
                self.lock_ttl_seconds // 3,
                _env_int(
                    "USAGE_REALTIME_LOCK_RENEW_SECONDS",
                    max(1, self.lock_ttl_seconds // 3),
                ),
            ),
        )
        self.directory_refresh_seconds = max(
            60, _env_int("USAGE_REALTIME_DIRECTORY_REFRESH_SECONDS", 300)
        )
        self.live_window_seconds = max(
            self.poll_seconds,
            _env_int("USAGE_REALTIME_LIVE_WINDOW_SECONDS", 60),
        )
        self.max_cursor_age_seconds = max(
            self.live_window_seconds,
            _env_int("USAGE_REALTIME_MAX_CURSOR_AGE_SECONDS", 900),
        )
        self.history_window_seconds = max(
            60, _env_int("USAGE_REALTIME_HISTORY_WINDOW_SECONDS", 3600)
        )
        self.backfill_pages_per_cycle = max(
            1, _env_int("USAGE_REALTIME_BACKFILL_PAGES_PER_CYCLE", 5)
        )
        # Only publish fully scanned windows that are safely behind upstream
        # writes. Dense windows are split before their watermark advances.
        self.settlement_delay_seconds = max(60, _env_int("USAGE_REALTIME_SETTLEMENT_DELAY_SECONDS", 180))
        self.settlement_max_pages = max(1, _env_int("USAGE_REALTIME_SETTLEMENT_MAX_PAGES", 20))
        self.settlement_min_window_seconds = max(1, _env_int("USAGE_REALTIME_SETTLEMENT_MIN_WINDOW_SECONDS", 5))
        self.directory: dict[str, Any] = {}
        self.token_maps: dict[str, dict[Any, Any]] = {}
        self.team_by_user: dict[tuple[str, str], tuple[str, str]] = {}
        self.current_day = usage_today()
        self._lock_renew_task: asyncio.Task[None] | None = None
        self._lock_lost = asyncio.Event()

    def realtime_poll_window(
        self, cursor: datetime | None, end_time: datetime
    ) -> tuple[datetime, datetime | None]:
        """Keep live polling bounded and return an optional history origin."""
        live_window = max(1, int(getattr(self, "live_window_seconds", 60)))
        max_age = max(live_window, int(getattr(self, "max_cursor_age_seconds", 900)))
        safe_start = end_time - timedelta(seconds=live_window)
        if cursor is None:
            return safe_start, None
        cursor = cursor.astimezone(timezone.utc)
        if (end_time - cursor).total_seconds() > max_age:
            return safe_start, cursor
        return cursor - timedelta(seconds=self.overlap_seconds), None

    def history_backfill_window(
        self, start_time: datetime, end_time: datetime
    ) -> tuple[datetime, datetime]:
        history_window = max(60, int(getattr(self, "history_window_seconds", 3600)))
        bounded_end = min(
            end_time,
            start_time + timedelta(seconds=history_window),
        )
        return start_time, bounded_end

    async def consume_refresh_requests(self) -> bool:
        """Process durable reader refresh requests outside the API request path."""

        claim = getattr(self.store, "claim_refresh_requests", None)
        finish = getattr(self.store, "finish_refresh_requests", None)
        if not callable(claim) or not callable(finish):
            return False
        requests = await claim(
            limit=max(1, _env_int("USAGE_REFRESH_QUEUE_BATCH_SIZE", 10))
        )
        if not requests:
            return False
        request_keys = [str(item["requestKey"]) for item in requests]
        start_date = min(str(item["startDate"]) for item in requests)
        end_date = max(str(item["endDate"]) for item in requests)
        try:
            result = await self.synchronizer.sync(start_date, end_date)
            success = result.get("status") in {"ok", "partial"}
            await finish(
                request_keys,
                success=success,
                error="" if success else "; ".join(result.get("errors") or ["sync failed"]),
            )
        except asyncio.CancelledError:
            # The realtime loop may cancel this operation when its short
            # background budget expires. Release the claim before propagating
            # cancellation so the request can be retried instead of sticking
            # in `running` forever.
            await asyncio.shield(
                finish(request_keys, success=False, error="CancelledError")
            )
            raise
        except Exception as exc:
            await finish(request_keys, success=False, error=exc.__class__.__name__)
            logger.exception(
                "realtime queued refresh failed start=%s end=%s", start_date, end_date
            )
        return True

    async def backfill_cost_aggregates(self) -> bool:
        next_range = getattr(self.store, "next_cost_api_backfill_range", None)
        rebuild = getattr(self.store, "rebuild_cost_api_daily", None)
        if not callable(next_range) or not callable(rebuild):
            return False
        window = await next_range(
            max(1, _env_int("COST_AGGREGATE_BACKFILL_DAYS_PER_BATCH", 7))
        )
        if not window:
            return False
        await rebuild(window["start_date"], window["end_date"])
        logger.info(
            "cost aggregate backfill start=%s end=%s",
            window["start_date"], window["end_date"],
        )
        return True

    async def _renew_worker_lock_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=self.lock_renew_seconds
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                renewed = await self.realtime.renew_worker_lock(
                    self.worker_id, self.lock_ttl_seconds
                )
            except Exception:
                logger.exception("realtime worker lock renewal failed")
                renewed = False
            if not renewed:
                logger.error("realtime worker lock was lost during renewal")
                self._lock_lost.set()
                self.stop_event.set()
                return

    async def _connect_repository(self) -> None:
        connect = getattr(self.synchronizer.organization_repository, "connect", None)
        if callable(connect):
            await connect()

    async def _refresh_directories(self, *, refresh_departments: bool = True) -> None:
        self.directory = await self.synchronizer._identity_directory()
        # Persist the upstream directory even in realtime-only mode.  The
        # regular snapshot synchronizer performs this after a full scan, but
        # realtime workers may run without that path ever executing.
        store = getattr(self, "store", None)
        directory_upsert = getattr(store, "upsert_identity_directory", None)
        directory_refresh = getattr(store, "refresh_usage_identity_columns", None)
        if callable(directory_upsert):
            for backend in self.client.backends:
                try:
                    users_loader = getattr(self.client, "users", None)
                    if not callable(users_loader):
                        continue
                    users = await users_loader(backend)
                    identities = []
                    for user in users or []:
                        user_id = str(user.get("user_id") or user.get("userId") or "").strip()
                        if not user_id:
                            continue
                        resolved = resolve_display_identity(
                            user_id=user_id,
                            user_record=user,
                            directory=self.directory,
                            backend_id=backend.id,
                        )
                        identities.append(
                            {
                                "userId": user_id,
                                "name": resolved.get("name") or user_id,
                                "email": resolved.get("email") or "",
                                "nameSource": resolved.get("nameSource") or "user_id",
                                "confidence": resolved.get("confidence") or "low",
                            }
                        )
                    if identities:
                        await directory_upsert(backend.id, identities)
                        if callable(directory_refresh):
                            await directory_refresh([backend.id])
                except Exception:
                    logger.exception("realtime identity directory refresh failed backend=%s", backend.id)
        identity_loader = getattr(getattr(self, "store", None), "identity_directory", None)
        if callable(identity_loader):
            try:
                stored = await identity_loader([backend.id for backend in self.client.backends])
                by_user = self.directory.setdefault("byUserId", {})
                for (backend_id, user_id), identity in stored.items():
                    existing = by_user.get(user_id) or {}
                    if identity.get("name") or identity.get("displayName"):
                        by_user[user_id] = {
                            **existing,
                            "name": existing.get("name") or identity.get("name") or identity.get("displayName"),
                            "email": existing.get("email") or identity.get("email") or identity.get("employeeEmail"),
                            "emailSource": existing.get("emailSource") or identity.get("nameSource") or "",
                        }
            except Exception:
                logger.exception("failed to load stored identity directory")
        self.token_maps = {
            backend.id: await self.synchronizer._token_attribution_map(backend.id)
            for backend in self.client.backends
        }
        self.team_by_user = await self._load_team_by_user()
        if refresh_departments:
            try:
                await self.synchronizer.sync_department_directories()
            except Exception:
                # Usage ingestion can continue with the last durable directory;
                # the next five-minute refresh will retry upstream department names.
                logger.exception("realtime department directory refresh failed")

    async def _load_team_by_user(self) -> dict[tuple[str, str], tuple[str, str]]:
        """Load each member's single latest team for realtime attribution backfill."""

        store = getattr(self, "store", None)
        loader = getattr(store, "latest_membership_teams", None) if store is not None else None
        if not callable(loader):
            return {}
        try:
            rows = await loader([backend.id for backend in self.client.backends])
        except Exception:
            logger.exception("latest membership teams load failed")
            return {}
        return UsageSynchronizer._latest_team_by_user(rows)

    def _enrich_event(
        self, backend: LiteLLMBackend, event: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = str(event.get("_userId") or "unattributed")
        resolved = resolve_display_identity(
            user_id=user_id,
            log_record=event,
            directory=self.directory,
            backend_id=backend.id,
        )
        enriched = {
            **event,
            "employeeEmail": str(resolved.get("email") or ""),
            "employeeName": str(resolved.get("name") or user_id),
            "emailSource": str(resolved.get("emailSource") or ""),
            "nameSource": str(resolved.get("nameSource") or "user_id"),
            "nameConfidence": str(resolved.get("confidence") or "low"),
        }
        self.synchronizer._reclassify_primary_her_usage(
            backend, [enriched], self.directory
        )
        mapping = self.token_maps.get(backend.id) or {}
        if mapping:
            self.synchronizer._apply_token_attribution([enriched], mapping)
        # 个人直接调用没有组织令牌与上游团队信息时仍是 unattributed；按团队目录
        # 唯一归属回填 team_id，仅用于团队/部门看板展示，不计费。
        if (
            not str(
                enriched.get("organizationId")
                or enriched.get("organization_id")
                or enriched.get("orgId")
                or enriched.get("org_id")
                or ""
            ).strip()
            and not str(
                enriched.get("teamId")
                or enriched.get("team_id")
                or enriched.get("tokenTeamId")
                or enriched.get("token_team_id")
                or enriched.get("userTeamId")
                or enriched.get("user_team_id")
                or ""
            ).strip()
        ):
            team = self.team_by_user.get((backend.id, user_id))
            if team is not None:
                enriched["teamId"] = team[0]
                enriched["attributionSource"] = "team_membership_backfill"
                enriched["billingEligible"] = False
        return enriched

    async def recover(self) -> None:
        started_at = datetime.now(timezone.utc)
        await self.store.update_worker_state(
            worker_id=self.worker_id,
            status="recovering",
            heartbeat_at=started_at,
            last_started_at=started_at,
            last_error="",
        )
        await self.realtime.set_ready(False)
        # Persist any stream entries left pending by a previous worker before
        # rebuilding today's Redis aggregates from PostgreSQL.
        await self.flush_archive()
        today = usage_today()
        self.current_day = today
        await self.realtime.clear_day(today)
        rows = await self.store.realtime_recovery_rows(today)
        for row in rows:
            await self.realtime.seed_aggregate(str(row["backendId"]), row)
        request_ids = await self.store.realtime_request_ids(
            datetime.now(timezone.utc) - timedelta(days=3)
        )
        await self.realtime.seed_request_ids(request_ids)
        # Directory names are already durable in PostgreSQL; refresh them
        # after the realtime cursor is running so a slow upstream directory
        # API cannot block cold-start recovery.
        # Legacy cursors/backfill checkpoints were based on rolling pages.
        # Closed-window settlements supersede them, so do not resume a cursor
        # that could carry a historical page gap into the new worker.
        for backend in self.client.backends:
            cursor_loader = getattr(self.realtime, "cursor", None)
            previous_cursor = await cursor_loader(backend.id) if callable(cursor_loader) else None
            await self.realtime.clear_backfill_checkpoint(backend.id)
            if previous_cursor is not None:
                live_start = datetime.now(timezone.utc) - timedelta(
                    seconds=self.live_window_seconds
                )
                if (
                    live_start - previous_cursor
                ).total_seconds() > self.max_cursor_age_seconds:
                    setter = getattr(self.realtime, "set_backfill_checkpoint", None)
                    if callable(setter):
                        await setter(
                            backend.id,
                            start_time=previous_cursor,
                            end_time=live_start,
                            next_page=1,
                        )
        # Replay today's closed intervals once after deployment. The audit table
        # deduplicates request IDs, so this safely fills earlier rolling-page gaps.
        if _env_int("USAGE_REALTIME_RESYNC_TODAY_ON_START", 1):
            try:
                await asyncio.wait_for(
                    self.resettle_today(datetime.now(timezone.utc)),
                    timeout=self.background_budget_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "HISTORICAL_BACKFILL_PARTIAL startup_resettle budget_seconds=%s",
                    self.background_budget_seconds,
                )
            except Exception:
                logger.exception("SNAPSHOT_REFRESH_FAILED startup_resettle")
        try:
            await asyncio.wait_for(
                self.settle_pending_windows(datetime.now(timezone.utc)),
                timeout=self.background_budget_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "HISTORICAL_BACKFILL_PARTIAL startup_settlement budget_seconds=%s",
                self.background_budget_seconds,
            )
        except Exception:
            logger.exception("SNAPSHOT_REFRESH_FAILED startup_settlement")
        await self.flush_archive()
        await self.publish_mirror(ready=True)
        await self.store.publish_realtime_coverage(
            today, [backend.id for backend in self.client.backends]
        )
        await self.realtime.set_ready(True)
        finished_at = datetime.now(timezone.utc)
        await self.store.update_worker_state(
            worker_id=self.worker_id,
            status="idle",
            heartbeat_at=finished_at,
            last_finished_at=finished_at,
            last_success_at=finished_at,
            snapshot_revision=str(await self.realtime.revision()),
            last_error="",
        )

    async def poll_backend(
        self, backend: LiteLLMBackend, end_time: datetime, *, lookback_minutes: int | None = None
    ) -> int:
        cursor = await self.realtime.cursor(backend.id)
        if lookback_minutes is not None:
            start_time = end_time - timedelta(minutes=lookback_minutes)
        else:
            start_time, _history_start = self.realtime_poll_window(cursor, end_time)
        events, complete = await self.client.incremental_events_from_logs(
            start_time, end_time, backend, page_size=100
        )
        if not complete:
            logger.warning("realtime window incomplete backend=%s", backend.id)
            return 0
        inserted = 0
        for event in events:
            accepted, _revision = await self.realtime.ingest_event(
                backend.id, self._enrich_event(backend, event)
            )
            inserted += int(accepted)
        if lookback_minutes is None:
            await self.realtime.set_cursor(backend.id, end_time)
        return inserted

    def settlement_target(self, now: datetime) -> datetime:
        """Return the exclusive end of the most recent safely closed minute."""

        closed_at = now.astimezone(timezone.utc) - timedelta(seconds=self.settlement_delay_seconds)
        return closed_at.replace(second=0, microsecond=0)

    async def settle_window(
        self,
        backend: LiteLLMBackend,
        start_time: datetime,
        end_time: datetime,
    ) -> bool:
        """Persist a closed interval, recursively splitting dense page sets."""

        events, complete = await self.client.settled_events_from_logs(
            start_time, end_time, backend, max_pages=self.settlement_max_pages
        )
        if complete:
            enriched_events = [{**self._enrich_event(backend, event), "backendId": backend.id} for event in events]
            await self.store.archive_realtime_events(enriched_events)
            record_segment = getattr(self.store, "record_realtime_settlement_segment", None)
            if callable(record_segment):
                await record_segment(
                    backend_id=backend.id,
                    start_time=start_time,
                    end_time=end_time,
                    status="complete",
                    request_count=len(enriched_events),
                    amount=sum(float(event.get("spend") or 0) for event in enriched_events),
                    completed_at=datetime.now(timezone.utc),
                )
            realtime = getattr(self, "realtime", None)
            if realtime is not None:
                for event in enriched_events:
                    await realtime.ingest_event(backend.id, event)
            return True
        seconds = (end_time - start_time).total_seconds()
        if seconds <= self.settlement_min_window_seconds:
            logger.warning(
                "settlement window remains incomplete backend=%s start=%s end=%s",
                backend.id, start_time.isoformat(), end_time.isoformat(),
            )
            return False
        midpoint = start_time + (end_time - start_time) / 2
        midpoint = midpoint.replace(microsecond=0)
        if midpoint <= start_time or midpoint >= end_time:
            return False
        return await self.settle_window(backend, start_time, midpoint) and await self.settle_window(backend, midpoint, end_time)

    async def settle_and_advance(
        self, backend: LiteLLMBackend, start_time: datetime, end_time: datetime
    ) -> bool:
        """Advance durable progress only after an entire closed interval lands."""

        complete = await self.settle_window(backend, start_time, end_time)
        set_status = getattr(self.realtime, "set_settlement_status", None)
        if not complete:
            record_segment = getattr(self.store, "record_realtime_settlement_segment", None)
            if callable(record_segment):
                await record_segment(
                    backend_id=backend.id,
                    start_time=start_time,
                    end_time=end_time,
                    status="incomplete",
                    retry_count=1,
                    error_summary="upstream page set incomplete or exceeded safety limit",
                )
            if callable(set_status):
                await set_status(backend.id, "verifying", "upstream page set incomplete or exceeded safety limit")
            return False
        advance = getattr(self.store, "advance_realtime_settlement", None)
        if callable(advance):
            await advance(backend.id, end_time)
        set_verified = getattr(self.realtime, "set_verified_through", None)
        if callable(set_verified):
            await set_verified(backend.id, end_time)
        if callable(set_status):
            await set_status(backend.id, "settled")
        return True

    def settlement_day_start(self, target: datetime) -> datetime:
        offset_minutes = _env_int("USAGE_TIMEZONE_OFFSET_MINUTES", -480)
        local_tz = timezone(timedelta(minutes=-offset_minutes))
        local_day = target.astimezone(local_tz).date()
        return datetime.combine(local_day, datetime.min.time(), tzinfo=local_tz).astimezone(timezone.utc)

    async def settle_pending_windows(self, now: datetime) -> None:
        """Settle complete one-minute windows through the shared watermark."""

        target = self.settlement_target(now)
        if target <= self.settlement_day_start(target):
            return
        loader = getattr(self.store, "realtime_settlement", None)
        for backend in self.client.backends:
            state = await loader(backend.id) if callable(loader) else None
            start = (state or {}).get("verifiedThrough") or self.settlement_day_start(target)
            if not isinstance(start, datetime):
                start = self.settlement_day_start(target)
            start = start.astimezone(timezone.utc).replace(second=0, microsecond=0)
            while start < target:
                end = min(start + timedelta(minutes=1), target)
                if not await self.settle_and_advance(backend, start, end):
                    break
                start = end

    async def resettle_today(self, now: datetime | None = None) -> None:
        """Rebuild the current local day through closed, auditable windows."""

        now = now or datetime.now(timezone.utc)
        target = self.settlement_target(now)
        if target <= self.settlement_day_start(target):
            return
        for backend in self.client.backends:
            start = self.settlement_day_start(target)
            while start < target:
                end = min(start + timedelta(minutes=1), target)
                if not await self.settle_and_advance(backend, start, end):
                    break
                start = end

    async def backfill_backend(self, backend: LiteLLMBackend) -> int:
        """Advance one bounded historical batch without delaying live polling."""

        checkpoint = await self.realtime.backfill_checkpoint(backend.id)
        if not checkpoint:
            return 0
        next_page = int(checkpoint["nextPage"])
        window_start, window_end = self.history_backfill_window(
            checkpoint["startTime"], checkpoint["endTime"]
        )
        events, complete = await self.client.incremental_events_from_logs(
            window_start,
            window_end,
            backend,
            page_size=100,
            start_page=next_page,
            max_pages=self.backfill_pages_per_cycle,
        )
        inserted = 0
        for event in events:
            accepted, _revision = await self.realtime.ingest_event(
                backend.id, self._enrich_event(backend, event)
            )
            inserted += int(accepted)
        if complete:
            live_cursor = await self.realtime.cursor(backend.id)
            next_start = window_end
            next_end = live_cursor or next_start
            if (next_end - next_start).total_seconds() > self.live_window_seconds:
                await self.realtime.set_backfill_checkpoint(
                    backend.id,
                    start_time=next_start,
                    end_time=next_end,
                    next_page=1,
                )
                logger.info(
                    "realtime backfill window advanced backend=%s start=%s end=%s",
                    backend.id,
                    next_start.isoformat(),
                    next_end.isoformat(),
                )
            else:
                await self.realtime.clear_backfill_checkpoint(backend.id)
                logger.info("realtime backfill complete backend=%s", backend.id)
        else:
            await self.realtime.set_backfill_checkpoint(
                backend.id,
                start_time=window_start,
                end_time=window_end,
                next_page=next_page + self.backfill_pages_per_cycle,
            )
            logger.info(
                "realtime backfill checkpoint backend=%s next_page=%s batch_pages=%s",
                backend.id,
                next_page + self.backfill_pages_per_cycle,
                self.backfill_pages_per_cycle,
            )
        return inserted

    async def backfill_once(self) -> int:
        inserted = 0
        for backend in self.client.backends:
            try:
                inserted += await self.backfill_backend(backend)
            except Exception:
                logger.exception("realtime backfill failed backend=%s", backend.id)
        return inserted

    async def poll_once(self, end_time: datetime) -> int:
        inserted = 0
        for backend in self.client.backends:
            try:
                inserted += await asyncio.wait_for(
                    self.poll_backend(backend, end_time),
                    timeout=self.backend_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "REALTIME_BACKEND_TIMEOUT backend=%s timeout_seconds=%s",
                    backend.id,
                    self.backend_timeout_seconds,
                )
            except Exception:
                logger.exception("REALTIME_POLL_FAILED backend=%s", backend.id)
        return inserted

    async def run_background_once(self, now: datetime) -> None:
        """Run at most one non-live task without delaying the next live cycle."""
        operations = (
            self.consume_refresh_requests,
            lambda: self.settle_pending_windows(now),
            self.backfill_cost_aggregates,
            self.backfill_once,
        )
        for operation in operations:
            try:
                await asyncio.wait_for(
                    operation(), timeout=self.background_budget_seconds
                )
                return
            except asyncio.TimeoutError:
                logger.warning(
                    "HISTORICAL_BACKFILL_PARTIAL operation=%s budget_seconds=%s",
                    getattr(operation, "__name__", "settlement"),
                    self.background_budget_seconds,
                )
                continue
            except Exception:
                logger.exception(
                    "SNAPSHOT_REFRESH_FAILED operation=%s",
                    getattr(operation, "__name__", "settlement"),
                )
                continue

    async def run_live_once(self, now: datetime) -> None:
        """Poll every backend with hard isolation, then publish one mirror."""
        await self.poll_once(now)
        await self.flush_archive()
        await self.publish_mirror()
        await self.store.publish_realtime_coverage(
            usage_today(), [backend.id for backend in self.client.backends]
        )

    async def flush_archive(self) -> int:
        total = 0
        affected_dates: set[date] = set()
        while True:
            messages = await self.realtime.read_archive_batch()
            if not messages:
                break
            valid = [event for _message_id, event in messages if event]
            # A database outage must leave the messages pending so the next
            # pass can retry them; acknowledging on failure would lose the
            # only durable copy outside Redis.
            await self.store.archive_realtime_events(valid)
            for event in valid:
                value = str(event.get("date") or event.get("usage_date") or event.get("eventTime") or event.get("event_time") or "")[:10]
                try:
                    affected_dates.add(date.fromisoformat(value))
                except ValueError:
                    continue
            await self.realtime.acknowledge([message_id for message_id, _ in messages])
            total += len(messages)
            if len(messages) < 200:
                break
        rebuild = getattr(self.store, "rebuild_cost_api_daily", None)
        if affected_dates and callable(rebuild):
            await rebuild(min(affected_dates), max(affected_dates))
        return total

    async def calibrate_previous_day(self) -> None:
        """Re-scan yesterday in hourly slices without blocking the live cursor."""

        local_today = usage_today()
        previous_day = local_today - timedelta(days=1)
        identities = await self.store.realtime_identity_map(previous_day)
        offset_minutes = _env_int("USAGE_TIMEZONE_OFFSET_MINUTES", -480)
        local_tz = timezone(timedelta(minutes=-offset_minutes))
        local_start = datetime.combine(
            previous_day, datetime.min.time(), tzinfo=local_tz
        )
        for hour in range(24):
            window_start = (local_start + timedelta(hours=hour)).astimezone(timezone.utc)
            window_end = (local_start + timedelta(hours=hour + 1)).astimezone(timezone.utc)
            for backend in self.client.backends:
                events, complete = await self.client.incremental_events_from_logs(
                    window_start, window_end, backend, page_size=100
                )
                if not complete:
                    continue
                await self.store.archive_realtime_events(
                    [
                        {**self._enrich_event(backend, event), "backendId": backend.id}
                        for event in events
                    ]
                )
        await self.store.finalize_realtime_day(previous_day, identities)

    async def publish_mirror(self, *, ready: bool = True) -> None:
        today = self.current_day
        rows = await self.store.realtime_event_rows(today)
        await self.store.replace_realtime_aggregates(today, rows)
        status = await self.realtime.status()
        await self.store.publish_realtime_state(
            today,
            ready=ready,
            revision=int(status.get("revision") or 0),
            latest_event_at=status.get("latestEventAt"),
        )

    async def run(self) -> None:
        await self.store.connect()
        await self.realtime.connect()
        await self._connect_repository()
        lock_retry_seconds = max(
            2, _env_int("USAGE_REALTIME_LOCK_RETRY_SECONDS", self.poll_seconds)
        )
        while not self.stop_event.is_set():
            if await self.realtime.acquire_worker_lock(self.worker_id):
                break
            logger.warning(
                "another realtime usage worker is active; retrying in %ss",
                lock_retry_seconds,
            )
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=lock_retry_seconds
                )
            except asyncio.TimeoutError:
                continue
        if self.stop_event.is_set():
            return
        self._lock_renew_task = asyncio.create_task(
            self._renew_worker_lock_loop(), name="usage-realtime-lock-renewal"
        )
        try:
            await self.recover()
            last_reconcile = datetime.now(timezone.utc)
            last_directory_refresh = last_reconcile
            while not self.stop_event.is_set():
                now = datetime.now(timezone.utc)
                if usage_today() != self.current_day:
                    await self.calibrate_previous_day()
                    await self.recover()
                    last_reconcile = now
                    last_directory_refresh = now
                if (now - last_directory_refresh).total_seconds() >= self.directory_refresh_seconds:
                    await self._refresh_directories()
                    last_directory_refresh = now
                # Live polling and publication always happen before any
                # settlement, refresh, cost, or historical backfill work.
                try:
                    await asyncio.wait_for(
                        self.run_live_once(now),
                        timeout=self.live_cycle_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "REALTIME_PUBLISH_FAILED timeout_seconds=%s",
                        self.live_cycle_timeout_seconds,
                    )
                except Exception:
                    logger.exception("REALTIME_PUBLISH_FAILED")
                if (now - last_reconcile).total_seconds() >= self.reconcile_seconds:
                    for backend in self.client.backends:
                        try:
                            await asyncio.wait_for(
                                self.poll_backend(backend, now, lookback_minutes=15),
                                timeout=self.backend_timeout_seconds,
                            )
                        except asyncio.TimeoutError:
                            logger.error("REALTIME_RECONCILE_TIMEOUT backend=%s", backend.id)
                        except Exception:
                            logger.exception("REALTIME_RECONCILE_FAILED backend=%s", backend.id)
                    last_reconcile = now
                await self.store.update_worker_state(
                    worker_id=self.worker_id,
                    status="idle",
                    heartbeat_at=datetime.now(timezone.utc),
                    last_finished_at=datetime.now(timezone.utc),
                    last_success_at=datetime.now(timezone.utc),
                    snapshot_revision=str(await self.realtime.revision()),
                    last_error="",
                )
                await self.run_background_once(now)
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=self.poll_seconds
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            self.stop_event.set()
            if self._lock_renew_task is not None:
                self._lock_renew_task.cancel()
                try:
                    await self._lock_renew_task
                except asyncio.CancelledError:
                    pass
                self._lock_renew_task = None
            await self.realtime.release_worker_lock(self.worker_id)
        if self._lock_lost.is_set():
            raise RuntimeError("realtime usage worker lock was lost")

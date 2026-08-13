from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .litellm_client import LiteLLMBackend, LiteLLMClient, usage_today
from .usage_realtime import UsageRealtimeStore
from .usage_store import UsageStore
from .usage_sync import UsageSynchronizer


logger = logging.getLogger("ai-token-dashboard.usage-realtime-worker")


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
        self.overlap_seconds = max(
            1, _env_int("USAGE_REALTIME_OVERLAP_SECONDS", 60)
        )
        self.reconcile_seconds = max(
            60, _env_int("USAGE_REALTIME_RECONCILE_INTERVAL_SECONDS", 300)
        )
        self.directory: dict[str, Any] = {}
        self.token_maps: dict[str, dict[Any, Any]] = {}
        self.current_day = usage_today()

    async def _connect_repository(self) -> None:
        connect = getattr(self.synchronizer.organization_repository, "connect", None)
        if callable(connect):
            await connect()

    async def _refresh_directories(self) -> None:
        self.directory = await self.synchronizer._identity_directory()
        self.token_maps = {
            backend.id: await self.synchronizer._token_attribution_map(backend.id)
            for backend in self.client.backends
        }

    def _enrich_event(
        self, backend: LiteLLMBackend, event: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = str(event.get("_userId") or "unattributed")
        resolved = self.synchronizer._apply_identity_directory(
            backend, user_id, {"name": "", "email": ""}, self.directory
        )
        enriched = {
            **event,
            "employeeEmail": str(resolved.get("email") or ""),
            "employeeName": str(resolved.get("name") or user_id),
            "emailSource": str(resolved.get("emailSource") or ""),
        }
        self.synchronizer._reclassify_primary_her_usage(
            backend, [enriched], self.directory
        )
        mapping = self.token_maps.get(backend.id) or {}
        if mapping:
            self.synchronizer._apply_token_attribution([enriched], mapping)
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
        await self._refresh_directories()
        today_start = datetime.combine(
            today, datetime.min.time(), tzinfo=timezone.utc
        )
        for backend in self.client.backends:
            last_archived = await self.store.latest_archived_event_at(backend.id)
            # The archive table spans all history. Never let an old event from
            # a previous day turn the first realtime request into a multi-day
            # scan; today's recovery is seeded from the database separately.
            cursor = max(last_archived, today_start) if last_archived else today_start
            await self.realtime.set_cursor(backend.id, cursor)
        await self.poll_once(datetime.now(timezone.utc))
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
            start_time = (cursor or end_time) - timedelta(seconds=self.overlap_seconds)
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

    async def poll_once(self, end_time: datetime) -> int:
        inserted = 0
        for backend in self.client.backends:
            try:
                inserted += await self.poll_backend(backend, end_time)
            except Exception:
                logger.exception("realtime poll failed backend=%s", backend.id)
        return inserted

    async def flush_archive(self) -> int:
        total = 0
        while True:
            messages = await self.realtime.read_archive_batch()
            if not messages:
                break
            valid = [event for _message_id, event in messages if event]
            # A database outage must leave the messages pending so the next
            # pass can retry them; acknowledging on failure would lose the
            # only durable copy outside Redis.
            await self.store.archive_realtime_events(valid)
            await self.realtime.acknowledge([message_id for message_id, _ in messages])
            total += len(messages)
            if len(messages) < 200:
                break
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
        today = usage_today()
        rows = await self.realtime.aggregate_rows(today)
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
        if not await self.realtime.acquire_worker_lock(self.worker_id):
            raise RuntimeError("another realtime usage worker is active")
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
                if (now - last_directory_refresh).total_seconds() >= 300:
                    await self._refresh_directories()
                    last_directory_refresh = now
                await self.poll_once(now)
                if (now - last_reconcile).total_seconds() >= self.reconcile_seconds:
                    for backend in self.client.backends:
                        try:
                            await self.poll_backend(backend, now, lookback_minutes=15)
                        except Exception:
                            logger.exception(
                                "realtime reconcile failed backend=%s", backend.id
                            )
                    last_reconcile = now
                await self.flush_archive()
                await self.publish_mirror()
                await self.store.publish_realtime_coverage(
                    usage_today(), [backend.id for backend in self.client.backends]
                )
                if not await self.realtime.renew_worker_lock(self.worker_id):
                    raise RuntimeError("realtime usage worker lock was lost")
                await self.store.update_worker_state(
                    worker_id=self.worker_id,
                    status="idle",
                    heartbeat_at=now,
                    last_finished_at=now,
                    last_success_at=now,
                    snapshot_revision=str(await self.realtime.revision()),
                    last_error="",
                )
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=self.poll_seconds
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            await self.realtime.release_worker_lock(self.worker_id)

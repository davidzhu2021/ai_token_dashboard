from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from .litellm_client import LiteLLMClient, usage_today
from .organization_repository import PostgreSQLOrganizationRepository
from .usage_store import UsageStore
from .usage_sync import UsageSynchronizer, run_sync_with_recent_refresh, run_usage_backfill_once
from .usage_realtime import UsageRealtimeStore, realtime_enabled
from .usage_realtime_worker import UsageRealtimeWorker, new_worker_id


logger = logging.getLogger("ai-token-dashboard.usage-worker")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class UsageSyncWorker:
    def __init__(
        self,
        client: LiteLLMClient,
        store: UsageStore,
        repository: Any | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.stop_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._current_status = "starting"

    @property
    def backend_ids(self) -> list[str]:
        return [backend.id for backend in self.client.backends]

    async def _heartbeat_loop(self) -> None:
        interval = max(5, _env_int("USAGE_SYNC_HEARTBEAT_INTERVAL_SECONDS", 30))
        while not self.stop_event.is_set():
            try:
                await self.store.heartbeat_worker(self.worker_id, self._current_status)
            except Exception:
                logger.exception("usage worker heartbeat failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _run_sync(self, days: int) -> dict[str, Any]:
        started_at = self.now()
        self._current_status = "running"
        await self.store.update_worker_state(
            worker_id=self.worker_id,
            status="running",
            heartbeat_at=started_at,
            last_started_at=started_at,
            last_error="",
        )
        try:
            result = await run_sync_with_recent_refresh(
                self.client,
                self.store,
                days,
                self.repository,
                UsageSynchronizer,
            )
            if result.get("status") in {"ok", "partial"} and isinstance(
                self.repository, PostgreSQLOrganizationRepository
            ):
                result["organizationBackfill"] = await run_usage_backfill_once(
                    self.client,
                    self.store,
                    self.repository,
                    max_windows=2,
                )
            finished_at = self.now()
            success = result.get("status") == "ok"
            revision = str(result.get("snapshotRevision") or "")
            self._current_status = "idle" if success else str(result.get("status") or "failed")
            await self.store.update_worker_state(
                worker_id=self.worker_id,
                status=self._current_status,
                heartbeat_at=finished_at,
                last_finished_at=finished_at,
                last_success_at=finished_at if success else None,
                last_error="" if success else "; ".join(result.get("errors") or []),
                snapshot_revision=revision or None,
            )
            return result
        except Exception as exc:
            finished_at = self.now()
            self._current_status = "failed"
            await self.store.update_worker_state(
                worker_id=self.worker_id,
                status="failed",
                heartbeat_at=finished_at,
                last_finished_at=finished_at,
                last_error=exc.__class__.__name__,
            )
            logger.exception("usage worker sync failed days=%s", days)
            return {"status": "failed", "errors": [exc.__class__.__name__]}

    async def startup_sync_days(self) -> int | None:
        initial_days = max(1, _env_int("USAGE_INITIAL_BACKFILL_DAYS", 90))
        recent_days = max(1, _env_int("USAGE_SYNC_RECENT_DAYS", 2))
        stale_after = max(60, _env_int("USAGE_SYNC_STARTUP_MAX_AGE_SECONDS", 1800))
        history_start, history_end = UsageSynchronizer.date_range(initial_days)
        if not await self.store.has_complete_coverage(
            history_start, history_end, self.backend_ids
        ):
            return initial_days
        last_success = await self.store.latest_success_at()
        if last_success is None or (self.now() - last_success).total_seconds() > stale_after:
            return recent_days
        return None

    async def consume_refresh_requests(self) -> bool:
        """Collapse queued reader refreshes into one bounded snapshot sync."""

        claim = getattr(self.store, "claim_refresh_requests", None)
        finish = getattr(self.store, "finish_refresh_requests", None)
        if not callable(claim) or not callable(finish):
            return False
        requests = await claim(limit=max(1, _env_int("USAGE_REFRESH_QUEUE_BATCH_SIZE", 10)))
        if not requests:
            return False
        request_keys = [str(item["requestKey"]) for item in requests]
        earliest = min(date.fromisoformat(str(item["startDate"])) for item in requests)
        days = max(1, (usage_today() - earliest).days + 1)
        days = min(days, max(1, _env_int("USAGE_INITIAL_BACKFILL_DAYS", 90)))
        result = await self._run_sync(days)
        success = result.get("status") in {"ok", "partial"}
        await finish(
            request_keys,
            success=success,
            error="" if success else "; ".join(result.get("errors") or ["sync failed"]),
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
            window["start_date"],
            window["end_date"],
        )
        return True

    def _intervals(self) -> tuple[int, int, int, int]:
        recent_days = max(1, _env_int("USAGE_SYNC_RECENT_DAYS", 2))
        calibration_days = max(recent_days, _env_int("USAGE_SYNC_CALIBRATION_DAYS", 3))
        refresh_interval = max(60, _env_int("USAGE_SYNC_INTERVAL_SECONDS", 1800))
        if os.getenv("ADMIN_OBSERVABILITY_DASHBOARDS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            # Stability snapshots have their own cadence; the worker still
            # publishes the same atomic snapshot so dashboard reads stay cheap.
            refresh_interval = min(
                refresh_interval,
                max(60, _env_int("STABILITY_SYNC_INTERVAL_SECONDS", refresh_interval)),
            )
        calibration_interval = max(
            refresh_interval,
            _env_int("USAGE_SYNC_CALIBRATION_INTERVAL_SECONDS", 21600),
        )
        return recent_days, calibration_days, refresh_interval, calibration_interval

    def due_sync(
        self,
        now: datetime,
        last_refresh: datetime,
        last_calibration: datetime,
    ) -> tuple[str, int] | None:
        """Pick the sync that is due, preferring the wider calibration window."""

        recent_days, calibration_days, refresh_interval, calibration_interval = self._intervals()
        if now >= last_calibration + timedelta(seconds=calibration_interval):
            return ("calibration", calibration_days)
        if now >= last_refresh + timedelta(seconds=refresh_interval):
            return ("refresh", recent_days)
        return None

    def seconds_until_next_sync(
        self,
        now: datetime,
        last_refresh: datetime,
        last_calibration: datetime,
    ) -> float:
        _, _, refresh_interval, calibration_interval = self._intervals()
        next_at = min(
            last_refresh + timedelta(seconds=refresh_interval),
            last_calibration + timedelta(seconds=calibration_interval),
        )
        return max(0.0, (next_at - now).total_seconds())

    async def run(self) -> None:
        await self.store.connect()
        if self.repository is not None:
            await self.repository.connect()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="usage-worker-heartbeat"
        )
        try:
            startup_days = await self.startup_sync_days()
            if startup_days is not None:
                await self._run_sync(startup_days)
                last_refresh = self.now()
            else:
                self._current_status = "idle"
                await self.store.heartbeat_worker(self.worker_id, "idle")
                # 启动时快照仍新鲜，跳过了同步。周期必须从上一次成功时刻起算，
                # 否则每次重启都把时钟推后一整个周期，快照年龄会超出新鲜度预算。
                last_refresh = await self.store.latest_success_at() or self.now()
            last_calibration = self.now()
            while not self.stop_event.is_set():
                if await self.backfill_cost_aggregates():
                    continue
                if await self.consume_refresh_requests():
                    last_refresh = self.now()
                    continue
                wait_seconds = self.seconds_until_next_sync(
                    self.now(), last_refresh, last_calibration
                )
                wait_seconds = min(
                    wait_seconds,
                    max(5, _env_int("USAGE_REFRESH_QUEUE_POLL_SECONDS", 15)),
                )
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=wait_seconds)
                    continue
                except asyncio.TimeoutError:
                    pass
                now = self.now()
                due = self.due_sync(now, last_refresh, last_calibration)
                if due is None:
                    continue
                kind, days = due
                # 一次同步失败不终止循环：状态已写入共享表，下一个周期继续重试。
                await self._run_sync(days)
                last_refresh = now
                if kind == "calibration":
                    last_calibration = now
        finally:
            self.stop_event.set()
            if self._heartbeat_task is not None:
                await self._heartbeat_task


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = UsageStore.from_environment()
    if store is None:
        raise RuntimeError("usage worker requires USAGE_SYNC_ENABLED=true and USAGE_DATABASE_URL")
    client = LiteLLMClient()
    repository = None
    if os.getenv("ORGANIZATION_MODE", "disabled").strip().lower() == "real":
        repository = PostgreSQLOrganizationRepository.from_environment()
    realtime = UsageRealtimeStore.from_environment()
    worker: Any
    if realtime_enabled() and realtime is not None:
        worker = UsageRealtimeWorker(
            client,
            store,
            realtime,
            repository,
            worker_id=new_worker_id(),
        )
    else:
        worker = UsageSyncWorker(client, store, repository)
    try:
        await worker.run()
    finally:
        await client.close()
        if repository is not None:
            await repository.close()
        if realtime is not None:
            await realtime.close()
        await store.close()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()

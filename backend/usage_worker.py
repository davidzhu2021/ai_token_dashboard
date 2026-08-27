from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
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


def _retry_delay_seconds(attempts: int) -> int:
    base = max(1, _env_int("USAGE_REFRESH_RETRY_BASE_SECONDS", 30))
    maximum = max(base, _env_int("USAGE_REFRESH_RETRY_MAX_SECONDS", 900))
    return min(maximum, base * (2 ** max(0, min(8, attempts - 1))))


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

    async def _realtime_settlement_lagging(self) -> bool:
        """Yield to the realtime worker while its verification watermark is behind."""
        if os.getenv("USAGE_REFRESH_REALTIME_GUARD_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
            return False
        realtime = getattr(self, "realtime", None)
        owns_realtime = False
        if realtime is None and os.getenv("USAGE_REDIS_URL", "").strip():
            try:
                realtime = UsageRealtimeStore(os.getenv("USAGE_REDIS_URL", "").strip())
                owns_realtime = True
            except Exception:
                return False
        if realtime is None:
            return False
        try:
            await realtime.connect()
            state = await realtime.status()
            threshold = max(60, _env_int("USAGE_REFRESH_DEFER_SETTLEMENT_LAG_SECONDS", 120))
            statuses = state.get("settlementStatuses") or {}
            if any(
                isinstance(value, dict) and str(value.get("status") or "") not in {"", "settled"}
                for value in statuses.values()
            ):
                return True
            verified = state.get("verifiedThrough") or {}
            now = self.now()
            for value in verified.values() if isinstance(verified, dict) else ():
                try:
                    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    if (now - parsed).total_seconds() > threshold:
                        return True
                except (TypeError, ValueError):
                    return True
            return False
        except Exception:
            # A Redis outage must not prevent the durable snapshot worker from recovering.
            return False
        finally:
            if owns_realtime:
                try:
                    await realtime.close()
                except Exception:
                    pass

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

    async def _run_sync(
        self, days: int, *, end_date: str | date | None = None
    ) -> dict[str, Any]:
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
                end_date=end_date,
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
        snapshot_state = getattr(self.store, "snapshot_state", None)
        published_at: datetime | None = None
        if callable(snapshot_state):
            state = await snapshot_state() or {}
            raw_published_at = state.get("publishedAt")
            if isinstance(raw_published_at, datetime):
                published_at = raw_published_at
        if published_at is None:
            # Compatibility with pre-publication stores that only expose the
            # worker operation timestamp.
            published_at = await self.store.latest_success_at()
        if published_at is None or (self.now() - published_at).total_seconds() > stale_after:
            return recent_days
        return None

    async def consume_refresh_requests(self) -> bool:
        """Collapse queued reader refreshes into one bounded snapshot sync."""

        claim = getattr(self.store, "claim_refresh_requests", None)
        finish = getattr(self.store, "finish_refresh_requests", None)
        if not callable(claim) or not callable(finish):
            return False
        if await self._realtime_settlement_lagging():
            logger.warning("deferring queued usage refresh while realtime settlement is lagging")
            return False
        stale_after_seconds = max(
            60, _env_int("USAGE_REFRESH_CLAIM_STALE_SECONDS", 300)
        )
        requests = await claim(
            limit=max(1, _env_int("USAGE_REFRESH_QUEUE_BATCH_SIZE", 10)),
            stale_after_seconds=stale_after_seconds,
        )
        if not requests:
            return False
        request_keys = [str(item["requestKey"]) for item in requests]
        earliest = min(date.fromisoformat(str(item["startDate"])) for item in requests)
        latest = max(
            date.fromisoformat(str(item.get("endDate") or item["startDate"]))
            for item in requests
        )
        # One idempotent sync covers the complete union of claimed windows.
        # Do not silently replace the requested end with today's date: historical
        # refreshes are allowed to target a bounded past range.
        days = max(1, (latest - earliest).days + 1)
        attempts = max(int(item.get("attempts") or 1) for item in requests)
        started_at = time.perf_counter()

        async def finish_requests(**kwargs: Any) -> None:
            """Persist queue outcome, tolerating pre-migration store doubles."""

            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            kwargs["duration_ms"] = duration_ms
            try:
                await finish(request_keys, **kwargs)
            except TypeError as exc:
                # Older test doubles/pre-migration adapters do not know the
                # optional duration field; do not lose the queue outcome.
                if "duration_ms" not in str(exc):
                    raise
                kwargs.pop("duration_ms", None)
                await finish(request_keys, **kwargs)
            logger.info(
                "usage refresh queue finished keys=%s status=%s duration_ms=%s attempts=%s",
                len(request_keys),
                "completed" if kwargs.get("success") else "pending",
                duration_ms,
                attempts,
            )

        try:
            timeout_seconds = max(1, _env_int("USAGE_REFRESH_TASK_TIMEOUT_SECONDS", 900))
            result = await asyncio.wait_for(
                self._run_sync(days, end_date=latest.isoformat()),
                timeout=timeout_seconds,
            )
            success = result.get("status") == "ok"
            finish_kwargs = {
                "success": success,
                "error": "" if success else "; ".join(result.get("errors") or ["sync failed"]),
            }
            if not success:
                finish_kwargs["retry_after_seconds"] = _retry_delay_seconds(attempts)
            await finish_requests(**finish_kwargs)
        except asyncio.CancelledError:
            await asyncio.shield(
                finish_requests(
                    success=False,
                    error="CancelledError",
                    retry_after_seconds=_retry_delay_seconds(attempts),
                )
            )
            return True
        except Exception as exc:
            await finish_requests(
                success=False,
                error=exc.__class__.__name__,
                retry_after_seconds=_retry_delay_seconds(attempts),
            )
            logger.exception("queued usage refresh failed")
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
            # Drain durable refresh requests before a potentially large startup
            # snapshot scan so an old queue cannot wait behind historical work.
            if await self.consume_refresh_requests():
                last_refresh = self.now()
            else:
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


def usage_worker_from_environment(
    client: LiteLLMClient,
    store: UsageStore,
    realtime: UsageRealtimeStore | None = None,
    repository: Any | None = None,
) -> Any:
    """Build the worker selected by the process environment.

    Compose runs realtime collection and snapshot refresh in separate services;
    keeping the selection here makes the split explicit and testable.
    """

    if realtime_enabled() and realtime is not None:
        return UsageRealtimeWorker(
            client,
            store,
            realtime,
            repository,
            worker_id=new_worker_id(),
        )
    worker = UsageSyncWorker(client, store, repository)
    # Snapshot workers use this object only for the realtime lag guard.
    worker.realtime = realtime
    return worker


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = UsageStore.from_environment()
    if store is None:
        raise RuntimeError("usage worker requires USAGE_SYNC_ENABLED=true and USAGE_DATABASE_URL")
    client = LiteLLMClient()
    repository = None
    if os.getenv("ORGANIZATION_MODE", "disabled").strip().lower() == "real":
        repository = PostgreSQLOrganizationRepository.from_environment()
    # The snapshot worker only reads realtime health, but still needs the
    # shared Redis status object to yield while settlement is backlogged.
    realtime = UsageRealtimeStore.from_environment(allow_disabled=True)
    worker = usage_worker_from_environment(client, store, realtime, repository)
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

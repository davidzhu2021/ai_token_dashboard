from datetime import date

from backend.usage_sync import _stability_scan_plan


def test_stability_scan_starts_with_today_for_fast_first_publish() -> None:
    desired_start = date(2026, 8, 6)
    desired_end = date(2026, 8, 12)

    assert _stability_scan_plan(desired_start, desired_end, None) == (
        desired_end,
        desired_end,
        desired_end,
        desired_end,
    )


def test_stability_scan_backfills_one_day_per_run() -> None:
    desired_start = date(2026, 8, 6)
    desired_end = date(2026, 8, 12)

    assert _stability_scan_plan(
        desired_start,
        desired_end,
        {"window_start": date(2026, 8, 10), "window_end": desired_end},
    ) == (
        date(2026, 8, 9),
        date(2026, 8, 9),
        date(2026, 8, 9),
        desired_end,
    )


def test_stability_scan_refreshes_today_after_window_is_complete() -> None:
    desired_start = date(2026, 8, 6)
    desired_end = date(2026, 8, 12)

    assert _stability_scan_plan(
        desired_start,
        desired_end,
        {"window_start": desired_start, "window_end": desired_end},
    ) == (desired_end, desired_end, desired_start, desired_end)


def test_stability_scan_retries_partial_day_before_extending_window() -> None:
    desired_start = date(2026, 8, 6)
    desired_end = date(2026, 8, 12)

    assert _stability_scan_plan(
        desired_start,
        desired_end,
        {
            "window_start": date(2026, 8, 9),
            "window_end": desired_end,
            "partial": True,
        },
    ) == (
        date(2026, 8, 9),
        date(2026, 8, 9),
        date(2026, 8, 9),
        desired_end,
    )

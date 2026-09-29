from datetime import date, datetime, timezone

from backend.usage_sync import _iso_date_text


def test_iso_date_text_normalizes_supported_date_values() -> None:
    assert _iso_date_text("2026-08-30") == "2026-08-30"
    assert _iso_date_text("2026-08-30T23:59:59+08:00") == "2026-08-30"
    assert _iso_date_text(date(2026, 8, 30)) == "2026-08-30"
    assert _iso_date_text(datetime(2026, 8, 30, 12, tzinfo=timezone.utc)) == "2026-08-30"

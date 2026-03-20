from datetime import date, datetime, timedelta, timezone
import os


DEFAULT_SCHEDULE_UTC_HOUR = 20
DEFAULT_SCHEDULE_UTC_MINUTE = 0


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def get_schedule_utc_hour() -> int:
    return _env_int("SCHEDULE_UTC_HOUR", DEFAULT_SCHEDULE_UTC_HOUR)


def get_schedule_utc_minute() -> int:
    return _env_int("SCHEDULE_UTC_MINUTE", DEFAULT_SCHEDULE_UTC_MINUTE)


def get_scheduled_reference_utc(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    reference = current.replace(
        hour=get_schedule_utc_hour(),
        minute=get_schedule_utc_minute(),
        second=0,
        microsecond=0,
    )
    if current < reference:
        reference -= timedelta(days=1)
    return reference


def get_target_dates_utc(window_days: int = 1, now: datetime | None = None) -> set[date]:
    reference_date = get_scheduled_reference_utc(now).date()
    return {
        reference_date - timedelta(days=offset)
        for offset in range(max(window_days, 0) + 1)
    }


def iso_target_dates_utc(window_days: int = 1, now: datetime | None = None) -> set[str]:
    return {d.isoformat() for d in get_target_dates_utc(window_days=window_days, now=now)}

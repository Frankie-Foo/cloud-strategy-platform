"""Honest market-data diagnostics without claiming an exchange-calendar guarantee."""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Literal

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def _nth_sunday(year: int, month: int, occurrence: int) -> date:
    weeks = calendar.monthcalendar(year, month)
    sundays = [week[calendar.SUNDAY] for week in weeks if week[calendar.SUNDAY]]
    return date(year, month, sundays[occurrence - 1])


def _eastern_timezone_for_utc(value: datetime) -> timezone:
    start_day = _nth_sunday(value.year, 3, 2)
    end_day = _nth_sunday(value.year, 11, 1)
    start_utc = datetime.combine(start_day, time(7, 0), tzinfo=UTC)
    end_utc = datetime.combine(end_day, time(6, 0), tzinfo=UTC)
    return timezone(timedelta(hours=-4 if start_utc <= value < end_utc else -5))


def _eastern_timezone_for_date(value: date) -> timezone:
    start_day = _nth_sunday(value.year, 3, 2)
    end_day = _nth_sunday(value.year, 11, 1)
    return timezone(timedelta(hours=-4 if start_day <= value < end_day else -5))


def _eastern(value: datetime) -> datetime:
    return value.astimezone(_eastern_timezone_for_utc(value))


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(UTC)


def describe_market_session(now_utc: datetime) -> dict[str, object]:
    local = _eastern(now_utc)
    clock = local.timetz().replace(tzinfo=None)
    if local.weekday() >= 5:
        session = "closed"
    elif time(4, 0) <= clock < REGULAR_OPEN:
        session = "pre_market"
    elif REGULAR_OPEN <= clock < REGULAR_CLOSE:
        session = "regular"
    elif REGULAR_CLOSE <= clock < time(20, 0):
        session = "after_hours"
    else:
        session = "closed"
    return {
        "session": session,
        "asof_utc": now_utc.isoformat(),
        "timezone": "America/New_York",
        "calendar_accuracy": "weekday schedule only; exchange holidays are unverified",
    }


def _regular_window(day: date) -> tuple[datetime, datetime]:
    eastern = _eastern_timezone_for_date(day)
    return (
        datetime.combine(day, REGULAR_OPEN, tzinfo=eastern).astimezone(UTC),
        datetime.combine(day, REGULAR_CLOSE, tzinfo=eastern).astimezone(UTC),
    )


def _requested_regular_dates(
    start_utc: datetime, end_utc: datetime
) -> tuple[date, ...]:
    start_day = _eastern(start_utc).date()
    end_day = _eastern(end_utc - timedelta(microseconds=1)).date()
    days: list[date] = []
    day = start_day
    while day <= end_day:
        if day.weekday() < 5:
            session_start, session_end = _regular_window(day)
            if max(start_utc, session_start) < min(end_utc, session_end):
                days.append(day)
        day += timedelta(days=1)
    return tuple(days)


def _regular_timestamp(value: datetime) -> bool:
    local = _eastern(value)
    clock = local.timetz().replace(tzinfo=None)
    return local.weekday() < 5 and REGULAR_OPEN <= clock < REGULAR_CLOSE


def _expected_regular_minutes(
    start_utc: datetime, end_utc: datetime
) -> tuple[datetime, ...]:
    expected: list[datetime] = []
    for day in _requested_regular_dates(start_utc, end_utc):
        session_start, session_end = _regular_window(day)
        cursor = max(start_utc, session_start).replace(second=0, microsecond=0)
        if cursor < start_utc:
            cursor += timedelta(minutes=1)
        stop = min(end_utc, session_end)
        while cursor < stop:
            expected.append(cursor)
            cursor += timedelta(minutes=1)
    return tuple(expected)


def _missing_intervals(
    missing_values: tuple[datetime, ...]
) -> tuple[dict[str, object], ...]:
    timestamps = sorted(set(missing_values))
    missing: list[dict[str, object]] = []
    if not timestamps:
        return ()
    start = timestamps[0]
    previous = start
    for current in timestamps[1:]:
        if current != previous + timedelta(minutes=1):
            missing.append(
                {
                    "start_utc": start.isoformat(),
                    "end_utc": (previous + timedelta(minutes=1)).isoformat(),
                    "missing_minutes": int(
                        ((previous + timedelta(minutes=1)) - start).total_seconds()
                        // 60
                    ),
                }
            )
            start = current
        previous = current
    missing.append(
        {
            "start_utc": start.isoformat(),
            "end_utc": (previous + timedelta(minutes=1)).isoformat(),
            "missing_minutes": int(
                ((previous + timedelta(minutes=1)) - start).total_seconds() // 60
            ),
        }
    )
    return tuple(missing)


def build_historical_coverage(
    *,
    kind: Literal["bars", "quotes"],
    symbols: tuple[str, ...],
    start_utc: datetime,
    end_utc: datetime,
    rows: tuple[dict[str, object], ...],
    now_utc: datetime,
) -> dict[str, object]:
    regular_dates = _requested_regular_dates(start_utc, end_utc)
    expected_regular_minutes = _expected_regular_minutes(start_utc, end_utc)
    symbol_results: list[dict[str, object]] = []
    overall_status = "observed"
    fallback_recommended = False
    for symbol in symbols:
        selected = [
            row for row in rows if str(row.get("symbol", "")).strip().upper() == symbol
        ]
        timestamps = [
            timestamp
            for row in selected
            if (timestamp := _utc(row.get("ts_utc"))) is not None
        ]
        observed_minutes = {
            value.replace(second=0, microsecond=0)
            for value in timestamps
            if _regular_timestamp(value)
        }
        missing_values = (
            tuple(
                value
                for value in expected_regular_minutes
                if value not in observed_minutes
            )
            if kind == "bars"
            else ()
        )
        missing = _missing_intervals(missing_values)
        observed_regular_dates = {_eastern(value).date() for value in observed_minutes}
        potential_missing_dates = [
            value.isoformat()
            for value in regular_dates
            if value not in observed_regular_dates
        ] if kind == "bars" else []
        reasons: list[str] = []
        if not selected:
            status = "empty"
            reasons.append("upstream_empty")
            if regular_dates:
                reasons.append("regular_session_missing")
                fallback_recommended = True
            else:
                reasons.append("outside_regular_session")
            overall_status = "empty"
        elif not timestamps:
            status = "invalid_timestamps"
            reasons.append("provider_timestamp_invalid")
            fallback_recommended = True
            if overall_status != "empty":
                overall_status = "invalid_timestamps"
        elif missing:
            status = "gaps_detected"
            reasons.append("minute_gaps_detected")
            fallback_recommended = True
            if overall_status not in {"empty", "invalid_timestamps"}:
                overall_status = "gaps_detected"
        else:
            status = "observed"
        reference = min(now_utc, end_utc)
        age_seconds = (
            None
            if not timestamps
            else max(0.0, (reference - max(timestamps)).total_seconds())
        )
        symbol_results.append(
            {
                "symbol": symbol,
                "status": status,
                "row_count": len(selected),
                "first_at_utc": None if not timestamps else min(timestamps).isoformat(),
                "last_at_utc": None if not timestamps else max(timestamps).isoformat(),
                "last_age_seconds": age_seconds,
                "missing_minute_count": sum(
                    value
                    for item in missing
                    if isinstance((value := item["missing_minutes"]), int)
                ),
                "missing_intervals": list(missing),
                "potential_missing_regular_dates": potential_missing_dates,
                "reason_codes": reasons,
            }
        )
    return {
        "status": overall_status,
        "source": "cloud.alpaca.market_data",
        "feed": "sip",
        "kind": kind,
        "requested_start_utc": start_utc.isoformat(),
        "requested_end_utc": end_utc.isoformat(),
        "calendar_basis": "observed regular-session continuity",
        "session_calendar_accuracy": (
            "America/New_York weekday schedule; exchange holidays are unverified"
        ),
        "fallback_recommended": fallback_recommended,
        "symbols": symbol_results,
    }

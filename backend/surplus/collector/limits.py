"""Pure parsers that turn each percent source's format into `LimitReading`s (spike 2).

Units differ per source, so normalisation happens here and nowhere else:
- probe `get_usage`: utilization 0-100, resets_at ISO-8601 string
- stream `rate_limit_event`: utilization 0-1, resetsAt unix seconds
- status line: used_percentage 0-100, resets_at unix seconds

The parsers are tolerant: unknown keys are ignored and every bucket may be null, because the probe API
is marked experimental. They raise `LimitFormatError` only when the payload has no usable structure.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from surplus.domain.models import LimitReading, ReadingSource, SurfaceShare, UsageSnapshot


class LimitFormatError(ValueError):
    """The payload does not look like any known version of the format."""


def _clamp_pct(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _parse_reset(value: Any) -> datetime | None:
    """Accept ISO strings or unix seconds; round to whole seconds (the server adds µs noise)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise LimitFormatError(f"unexpected reset value {value!r}")
    if isinstance(value, int | float):
        dt = datetime.fromtimestamp(float(value), tz=UTC)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LimitFormatError(f"bad timestamp {value!r}") from exc
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    else:
        raise LimitFormatError(f"unexpected reset value {value!r}")
    seconds = round(dt.timestamp())
    return datetime.fromtimestamp(seconds, tz=UTC)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


# ---------------------------------------------------------------- probe (get_usage)


def parse_get_usage(payload: Mapping[str, Any], ts: datetime) -> UsageSnapshot:
    """Parse the inner `response` object of a `get_usage` control_response."""
    rate_limits = payload.get("rate_limits")
    if payload.get("rate_limits_available") is False:
        return UsageSnapshot(ts=ts, subscription_type=payload.get("subscription_type"), readings=[])
    if not isinstance(rate_limits, Mapping):
        raise LimitFormatError("get_usage response has no rate_limits object")

    readings: list[LimitReading] = []
    for bucket, entry in rate_limits.items():
        if not isinstance(entry, Mapping):
            continue  # null buckets, extra_usage scalars, unknown shapes
        util = _number(entry.get("utilization"))
        if util is None or "resets_at" not in entry:
            continue
        readings.append(
            LimitReading(
                ts=ts,
                bucket=str(bucket),
                used_pct=_clamp_pct(util),
                resets_at=_parse_reset(entry.get("resets_at")),
                source=ReadingSource.PROBE,
            )
        )

    for scoped in rate_limits.get("model_scoped") or []:
        if not isinstance(scoped, Mapping):
            continue
        util = _number(scoped.get("utilization"))
        name = scoped.get("display_name")
        if util is None or not isinstance(name, str):
            continue
        readings.append(
            LimitReading(
                ts=ts,
                bucket=f"weekly_model:{name.strip().lower()}",
                used_pct=_clamp_pct(util),
                resets_at=_parse_reset(scoped.get("resets_at")),
                source=ReadingSource.PROBE,
            )
        )

    breakdown: list[SurfaceShare] = []
    window_start: datetime | None = None
    raw_breakdown = rate_limits.get("seven_day_breakdown") or payload.get("seven_day_breakdown")
    if isinstance(raw_breakdown, Mapping):
        window_start = _parse_reset(raw_breakdown.get("window_started_at"))
        for row in raw_breakdown.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            share = _number(row.get("percent_of_limit"))
            if share is None or not isinstance(row.get("key"), str):
                continue
            breakdown.append(
                SurfaceShare(
                    key=row["key"],
                    display_name=str(row.get("display_name") or row["key"]),
                    pct_of_limit=share,
                )
            )

    sub = payload.get("subscription_type")
    return UsageSnapshot(
        ts=ts,
        subscription_type=sub if isinstance(sub, str) else None,
        readings=readings,
        weekly_breakdown=breakdown,
        weekly_window_started_at=window_start,
    )


# ---------------------------------------------------------------- stream rate_limit_event


def parse_rate_limit_event(event: Mapping[str, Any], ts: datetime) -> list[LimitReading]:
    """Parse a stream-json `rate_limit_event` (utilisation is a 0-1 fraction there)."""
    info = event.get("rate_limit_info")
    if not isinstance(info, Mapping):
        raise LimitFormatError("rate_limit_event without rate_limit_info")
    readings: list[LimitReading] = []
    windows = info.get("unifiedWindows")
    if isinstance(windows, Mapping):
        for bucket, w in windows.items():
            if not isinstance(w, Mapping):
                continue
            util = _number(w.get("utilization"))
            if util is None:
                continue
            readings.append(
                LimitReading(
                    ts=ts,
                    bucket=str(bucket),
                    used_pct=_clamp_pct(util * 100.0),
                    resets_at=_parse_reset(w.get("resetsAt")),
                    source=ReadingSource.STREAM,
                    status=w.get("status") if isinstance(w.get("status"), str) else None,
                )
            )
    if not readings:
        # Older shape: one bucket per event.
        util = _number(info.get("utilization"))
        bucket = info.get("rateLimitType")
        if util is not None and isinstance(bucket, str):
            readings.append(
                LimitReading(
                    ts=ts,
                    bucket=bucket,
                    used_pct=_clamp_pct(util * 100.0),
                    resets_at=_parse_reset(info.get("resetsAt")),
                    source=ReadingSource.STREAM,
                    status=info.get("status") if isinstance(info.get("status"), str) else None,
                )
            )
    return readings


# ---------------------------------------------------------------- status line


def parse_statusline(payload: Mapping[str, Any], ts: datetime) -> list[LimitReading]:
    """Parse the JSON Claude Code pipes into a status-line command."""
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, Mapping):
        return []  # API-key sessions or older CLIs: nothing to read, not an error
    readings: list[LimitReading] = []
    for bucket, entry in rate_limits.items():
        if not isinstance(entry, Mapping) or bucket == "spend_limit":  # spend is money, not a quota bucket
            continue
        pct = _number(entry.get("used_percentage"))
        if pct is None:
            continue
        readings.append(
            LimitReading(
                ts=ts,
                bucket=str(bucket),
                used_pct=_clamp_pct(pct),
                resets_at=_parse_reset(entry.get("resets_at")),
                source=ReadingSource.STATUSLINE,
            )
        )
    return readings

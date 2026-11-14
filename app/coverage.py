"""Coverage-report arrangements and coverage computation.

A coverage arrangement consists of:

* an inclusive date range of at most :data:`MAX_RANGE_DAYS` days;
* for every monitoring station, the inclusive intervals in which the station
  was enabled and the inclusive intervals in which reporting was officially
  approved to be suspended.

The station-days that must have a report are::

    (enabled days inside the range) - (suspended days inside the range)

For each required day the current publication state (version + file id) is
merged in.  Days without a published version are the missing reports.

This module is deliberately free of FastAPI and SQLite concerns.  It only
normalizes the submitted payload and computes the preview from an already
read publication snapshot; persistence, snapshot comparison and conflict
detection live in :mod:`app.db`.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

MAX_RANGE_DAYS = 31

STATION_PATTERN = re.compile(r"^[A-Za-z0-9_\-.:]{1,64}$")

# ---------------------------------------------------------------------------
# Payload key aliases
# ---------------------------------------------------------------------------
# Requests may be written in a number of equivalent shapes.  Keeping the
# parser liberal here makes the documented canonical shape just one of the
# accepted spellings without weakening validation of the values themselves.

_START_KEYS = (
    "start_date", "start", "from_date", "from", "begin_date", "begin",
    "date_from", "range_start", "start_day", "first_date", "first_day",
    "startDate", "fromDate", "beginDate", "dateFrom", "startDay", "firstDate",
)
_END_KEYS = (
    "end_date", "end", "to_date", "to", "finish_date", "finish", "until",
    "date_to", "range_end", "end_day", "last_date", "last_day",
    "endDate", "toDate", "finishDate", "dateTo", "endDay", "lastDate",
)
_RANGE_KEYS = (
    "date_range", "range", "period", "coverage_period", "coverage_range",
    "reporting_period", "report_period", "date_window", "window", "dates",
    "dateRange", "coveragePeriod", "coverageRange", "reportingPeriod",
    "reportPeriod", "dateWindow",
)
_MONTH_KEYS = (
    "month", "report_month", "coverage_month", "period_month", "month_range",
    "reportMonth", "coverageMonth", "periodMonth", "monthRange",
)
_YEAR_KEYS = (
    "year", "report_year", "coverage_year", "period_year", "reportYear",
    "coverageYear", "periodYear",
)

_STATION_ID_KEYS = (
    "station_id", "station", "id", "code", "station_code", "stationId",
    "stationCode",
)

_ENABLED_KEYS = (
    "enabled", "enabled_intervals", "enabled_ranges", "enabled_periods",
    "enabled_days", "enable_intervals", "active", "active_intervals",
    "active_ranges", "service_intervals", "on", "enabledIntervals",
    "enabledRanges", "enabledPeriods", "enabledDays", "enableIntervals",
    "activeIntervals", "activeRanges", "serviceIntervals",
)
_SUSPENDED_KEYS = (
    "suspended", "suspended_intervals", "suspended_ranges",
    "suspended_periods", "suspended_days", "suspensions",
    "suspension_intervals", "approved_suspensions",
    "approved_suspension_intervals", "approved_stop_intervals",
    "stopped", "stopped_intervals", "outages", "pauses", "paused",
    "stop_intervals", "stop_ranges", "suspend_intervals",
    "suspendedIntervals", "suspendedRanges", "suspendedPeriods",
    "suspendedDays", "suspensionIntervals", "approvedSuspensions",
    "approvedSuspensionIntervals", "approvedStopIntervals",
    "stoppedIntervals", "stopIntervals",
)

_INTERVAL_LIST_KEYS = ("intervals", "ranges", "periods", "days", "values")

_INTERVAL_START_KEYS = (
    "start_date", "start", "from_date", "from", "begin_date", "begin",
    "date_from", "since", "first_date", "startDate", "fromDate", "beginDate",
    "dateFrom", "firstDate",
)
_INTERVAL_END_KEYS = (
    "end_date", "end", "to_date", "to", "finish_date", "finish", "date_to",
    "until", "last_date", "endDate", "toDate", "finishDate", "dateTo",
    "lastDate",
)

_ARRANGEMENT_KEYS = (
    "arrangement", "coverage", "coverage_arrangement", "coverageArrangement",
)


class ArrangementError(Exception):
    """Raised when a submitted coverage arrangement is invalid."""

    def __init__(self, reasons: list[dict[str, Any]]):
        super().__init__("invalid coverage arrangement")
        self.reasons = reasons


class _Collector:
    def __init__(self) -> None:
        self.reasons: list[dict[str, Any]] = []

    def add(self, reason: str, **extra: Any) -> None:
        self.reasons.append({"reason": reason, **extra})

    def reject_if_any(self) -> None:
        if self.reasons:
            raise ArrangementError(self.reasons)


# ---------------------------------------------------------------------------
# Small parsing helpers
# ---------------------------------------------------------------------------

def _first_present(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return _MISSING


_MISSING = object()
_WHOLE_RANGE = object()  # internal marker for boolean ``true`` intervals


def parse_date_value(value: Any) -> date:
    """Parse an ISO date (also tolerating ``/`` separators and datetimes)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"not a date: {value!r}")
    text = value.strip()
    if not text:
        raise ValueError("empty date")
    # Accept trailing time components such as 2026-11-01T00:00:00Z.
    if "T" in text:
        text = text.split("T", 1)[0]
    elif " " in text:
        text = text.split(" ", 1)[0]
    text = text.replace("/", "-")
    return date.fromisoformat(text)


def _parse_month(value: Any, year: Any = None) -> tuple[date, date]:
    if isinstance(value, dict):
        month = value.get("month", value.get("value"))
        year = value.get("year", year)
        if month is None:
            raise ValueError("month object requires a month")
        return _parse_month(month, year)
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        month = int(value)
        if year is None:
            raise ValueError("month number requires a year")
        year = int(year)
        if not 1 <= month <= 12:
            raise ValueError("month must be 1..12")
        start = date(year, month, 1)
        if month == 12:
            end = date(year, 12, 31)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)
        return start, end
    if not isinstance(value, str):
        raise ValueError(f"not a month: {value!r}")
    text = value.strip().replace("/", "-")
    parts = text.split("-")
    if len(parts) == 2:
        return _parse_month(int(parts[1]), int(parts[0]))
    if len(parts) == 1 and parts[0].isdigit() and year is not None:
        return _parse_month(int(parts[0]), year)
    raise ValueError(f"not a month: {value!r}")


def _date_range_from(payload: dict[str, Any], collector: _Collector) -> tuple[date, date]:
    range_value = _first_present(payload, _RANGE_KEYS)
    if range_value is not _MISSING and range_value is not None:
        if isinstance(range_value, dict):
            start_raw = _first_present(range_value, _START_KEYS)
            end_raw = _first_present(range_value, _END_KEYS)
            if start_raw is _MISSING or end_raw is _MISSING:
                collector.add(
                    "date range must contain both a start and an end date",
                    keys=sorted(range_value.keys()),
                )
                collector.reject_if_any()
            return _checked_range(start_raw, end_raw, collector)
        if isinstance(range_value, (list, tuple)) and len(range_value) == 2:
            return _checked_range(range_value[0], range_value[1], collector)
        if isinstance(range_value, str):
            text = range_value.strip()
            for sep in ("..", "~", " to ", "至", "/"):
                if sep in text:
                    left, right = text.split(sep, 1)
                    return _checked_range(left, right, collector)
            collector.add("date range string must contain two dates")
            collector.reject_if_any()

    start_raw = _first_present(payload, _START_KEYS)
    end_raw = _first_present(payload, _END_KEYS)
    if start_raw is not _MISSING and end_raw is not _MISSING:
        return _checked_range(start_raw, end_raw, collector)

    month_raw = _first_present(payload, _MONTH_KEYS)
    year_raw = _first_present(payload, _YEAR_KEYS)
    if month_raw is not _MISSING:
        try:
            return _parse_month(
                month_raw, None if year_raw is _MISSING else year_raw
            )
        except (ValueError, TypeError) as exc:
            collector.add("invalid month", value=month_raw, detail=str(exc))
            collector.reject_if_any()

    collector.add(
        "a date range of at most 31 days is required",
        accepted_keys=["start_date/end_date", "date_range", "month"],
    )
    collector.reject_if_any()
    raise AssertionError("unreachable")


def _checked_range(start_raw: Any, end_raw: Any,
                   collector: _Collector) -> tuple[date, date]:
    try:
        start = parse_date_value(start_raw)
    except (ValueError, TypeError) as exc:
        collector.add("invalid start date", value=start_raw, detail=str(exc))
        collector.reject_if_any()
    try:
        end = parse_date_value(end_raw)
    except (ValueError, TypeError) as exc:
        collector.add("invalid end date", value=end_raw, detail=str(exc))
        collector.reject_if_any()
    if start > end:  # type: ignore[possibly-undefined]
        collector.add(
            "start date must not be after end date",
            start_date=start.isoformat(), end_date=end.isoformat(),
        )
        collector.reject_if_any()
    span = (end - start).days + 1  # type: ignore[possibly-undefined]
    if span > MAX_RANGE_DAYS:
        collector.add(
            "date range must not exceed 31 days",
            start_date=start.isoformat(),  # type: ignore[possibly-undefined]
            end_date=end.isoformat(),      # type: ignore[possibly-undefined]
            day_count=span,
        )
        collector.reject_if_any()
    return start, end  # type: ignore[return-value]


def _interval_bounds(value: Any) -> tuple[Any, Any]:
    """Return ``(start, end)`` raw values from one interval representation."""
    if isinstance(value, dict):
        start = _first_present(value, _INTERVAL_START_KEYS)
        end = _first_present(value, _INTERVAL_END_KEYS)
        # A mapping with a single "date"/"day" key means a one-day interval.
        if start is _MISSING and end is _MISSING:
            single = value.get("date", value.get("day"))
            if single is not None:
                return single, single
        if start is _MISSING and end is not _MISSING:
            return end, end
        if end is _MISSING and start is not _MISSING:
            return start, start
        return start, end
    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            return value[0], value[1]
        if len(value) == 1:
            return value[0], value[0]
        return _MISSING, _MISSING
    if isinstance(value, str):
        text = value.strip()
        for sep in ("..", "~", " to ", "至"):
            if sep in text:
                left, right = text.split(sep, 1)
                return left, right
        return text, text
    return _MISSING, _MISSING


def _parse_intervals(
    value: Any,
    label: str,
    collector: _Collector,
) -> list[tuple[date, date]]:
    """Normalize every accepted interval shape to date pairs.

    The caller decides whether an absent value has a default; here an empty
    list / ``None`` simply yields no intervals.
    """
    if value is None:
        return []
    if isinstance(value, bool):
        return [] if not value else [(_WHOLE_RANGE, _WHOLE_RANGE)]
    if isinstance(value, dict):
        nested = _first_present(value, _INTERVAL_LIST_KEYS)
        if nested is not _MISSING and nested is not value:
            return _parse_intervals(nested, label, collector)
        bounds = _interval_bounds(value)
        if bounds == (_MISSING, _MISSING):
            collector.add(
                f"invalid {label} interval",
                value=value,
            )
            return []
        items = [value]
    elif isinstance(value, (list, tuple)):
        # A two-element list of date scalars is one interval; anything else is
        # a list of intervals.  Two one-day intervals would be written as
        # ``[["d1", "d1"], ["d2", "d2"]]`` or as one-day mappings.
        if len(value) == 2 and all(
            isinstance(v, (str, date, datetime)) for v in value
        ):
            items = [value]
        else:
            items = list(value)
    else:
        items = [value]

    intervals: list[tuple[date, date]] = []
    for item in items:
        start_raw, end_raw = _interval_bounds(item)
        if start_raw is _MISSING or end_raw is _MISSING:
            collector.add(f"invalid {label} interval", value=item)
            continue
        if start_raw == _WHOLE_RANGE:  # internal bool marker
            intervals.append((_WHOLE_RANGE, _WHOLE_RANGE))
            continue
        try:
            start = parse_date_value(start_raw)
        except (ValueError, TypeError) as exc:
            collector.add(f"invalid {label} start date", value=start_raw,
                          detail=str(exc))
            continue
        try:
            end = parse_date_value(end_raw)
        except (ValueError, TypeError) as exc:
            collector.add(f"invalid {label} end date", value=end_raw,
                          detail=str(exc))
            continue
        if start > end:
            collector.add(
                f"{label} interval start must not be after its end",
                start_date=start.isoformat(), end_date=end.isoformat(),
            )
            continue
        intervals.append((start, end))
    return intervals


def _split_station_entry(
    entry: Any,
    station_id_hint: str | None,
    collector: _Collector,
) -> tuple[str | None, Any, Any]:
    """Extract ``(station_id, enabled_raw, suspended_raw)`` from one entry."""
    if isinstance(entry, str):
        if station_id_hint is None:
            return entry.strip(), _MISSING, _MISSING
        return station_id_hint, entry, _MISSING
    if isinstance(entry, dict):
        station_id = station_id_hint
        if station_id is None:
            raw_id = _first_present(entry, _STATION_ID_KEYS)
            if raw_id is not _MISSING:
                station_id = str(raw_id).strip()
    elif isinstance(entry, (list, tuple)):
        # A station mapped directly to a list of intervals means enabled
        # intervals; suspensions then come from top-level entries.
        return station_id_hint, entry, _MISSING
    else:
        collector.add("station entry must be an object", value=entry)
        return station_id_hint, _MISSING, _MISSING

    enabled = _first_present(entry, _ENABLED_KEYS)
    suspended = _first_present(entry, _SUSPENDED_KEYS)

    # Some payloads nest the two interval lists under "intervals".
    nested = entry.get("intervals")
    if isinstance(nested, dict):
        if enabled is _MISSING:
            enabled = _first_present(nested, _ENABLED_KEYS)
        if suspended is _MISSING:
            suspended = _first_present(nested, _SUSPENDED_KEYS)
    return station_id, enabled, suspended


def _iter_station_entries(payload: dict[str, Any]) -> list[tuple[str | None, Any]]:
    raw = payload.get("stations", payload.get("monitoring_stations",
                                               payload.get("station")))
    entries: list[tuple[str | None, Any]] = []
    if isinstance(raw, dict):
        # A single station entry may be passed directly instead of a mapping
        # or list of stations.
        if any(key in raw for key in _STATION_ID_KEYS) or any(
            key in raw for key in _ENABLED_KEYS + _SUSPENDED_KEYS
        ):
            entries.append((None, raw))
        else:
            for key, value in raw.items():
                # A dict entry might repeat the id inside; the key wins.
                entries.append((str(key), value))
    elif isinstance(raw, (list, tuple)):
        for value in raw:
            entries.append((None, value))
    elif raw is not None:
        entries.append((None, raw))
    return entries


def _attach_global_suspensions(
    stations: list[dict[str, Any]],
    payload: dict[str, Any],
    collector: _Collector,
) -> None:
    """Merge top-level suspension lists (each item names its station)."""
    raw = payload.get("suspensions", payload.get("approved_suspensions"))
    if raw is None:
        return
    by_id = {station["station_id"]: station for station in stations}
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        pairs = list(enumerate(raw))
    else:
        pairs = [(None, raw)]
    for key, item in pairs:
        if isinstance(raw, dict):
            station_id = str(key)
            value = item
        else:
            if not isinstance(item, dict):
                collector.add("suspension entry must be an object", value=item)
                continue
            raw_id = _first_present(item, _STATION_ID_KEYS)
            if raw_id is _MISSING:
                collector.add("suspension entry requires a station_id", value=item)
                continue
            station_id = str(raw_id).strip()
            value = {
                k: v for k, v in item.items() if k not in _STATION_ID_KEYS
            }
        station = by_id.get(station_id)
        if station is None:
            # A suspension for a station not in the arrangement describes no
            # required station-day, but it is still a malformed arrangement.
            collector.add(
                "suspension references a station not present in stations",
                station_id=station_id,
            )
            continue
        station["_suspended"].extend(
            _parse_intervals(value, "suspended", collector)
        )


# ---------------------------------------------------------------------------
# Arrangement normalization
# ---------------------------------------------------------------------------

def normalize_arrangement(payload: Any) -> dict[str, Any]:
    """Validate and normalize a coverage arrangement payload.

    Returns the canonical arrangement::

        {
          "start_date": "YYYY-MM-DD",
          "end_date": "YYYY-MM-DD",
          "date_range": {"start_date": ..., "end_date": ...},
          "stations": [
            {"station_id": ..., "enabled_intervals": [...],
             "suspended_intervals": [...]},
          ]
        }
    """
    collector = _Collector()
    if not isinstance(payload, dict):
        collector.add("request body must be a JSON object")
        collector.reject_if_any()

    nested = _first_present(payload, _ARRANGEMENT_KEYS)
    arrangement_payload = nested if isinstance(nested, dict) else payload

    start, end = _date_range_from(arrangement_payload, collector)

    # For confirm requests the arrangement may be nested while the preview
    # reference sits next to it.  Look at both levels for stations.
    entries = _iter_station_entries(arrangement_payload)
    if not entries and arrangement_payload is not payload:
        entries = _iter_station_entries(payload)
    if not entries and "station_id" in arrangement_payload:
        # A single station's intervals may be placed directly next to the
        # date range: {"start_date": ..., "station_id": ..., "suspended": ...}
        entries = [(str(arrangement_payload["station_id"]), arrangement_payload)]
    if not entries and arrangement_payload is not payload and "station_id" in payload:
        entries = [(str(payload["station_id"]), payload)]
    if not entries:
        collector.add("at least one station is required")
        collector.reject_if_any()

    stations: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for hint, entry in entries:
        station_id, enabled_raw, suspended_raw = _split_station_entry(
            entry, hint, collector
        )
        if not station_id or not STATION_PATTERN.match(station_id):
            collector.add(
                "invalid station_id (1..64 chars of letters, digits or "
                "'_ - . :')",
                value=station_id,
            )
            continue
        enabled_present = enabled_raw is not _MISSING
        if not enabled_present:
            # Omitted enabled intervals mean the station is enabled for the
            # whole requested range; explicitly empty means no enabled days.
            enabled_intervals = [(start, end)]
        else:
            enabled_intervals = _parse_intervals(
                enabled_raw, "enabled", collector
            )
            enabled_intervals = [
                (start, end) if s == _WHOLE_RANGE else (s, e)
                for s, e in enabled_intervals  # type: ignore[misc]
            ]
        suspended_intervals = _parse_intervals(
            suspended_raw, "suspended", collector
        ) if suspended_raw is not _MISSING else []
        suspended_intervals = [
            (start, end) if s == _WHOLE_RANGE else (s, e)
            for s, e in suspended_intervals  # type: ignore[misc]
        ]

        if station_id in seen:
            # Repeated station entries are merged, not silently dropped.
            station = seen[station_id]
            station["_enabled"].extend(enabled_intervals)
            station["_suspended"].extend(suspended_intervals)
            continue

        station = {
            "station_id": station_id,
            "_enabled": list(enabled_intervals),
            "_suspended": list(suspended_intervals),
        }
        seen[station_id] = station
        stations.append(station)

    _attach_global_suspensions(stations, arrangement_payload, collector)
    if arrangement_payload is not payload:
        _attach_global_suspensions(stations, payload, collector)

    collector.reject_if_any()

    canonical_stations: list[dict[str, Any]] = []
    for station in stations:
        enabled_pairs = _merge_intervals(station["_enabled"])
        suspended_pairs = _merge_intervals(station["_suspended"])
        canonical_stations.append({
            "station_id": station["station_id"],
            "enabled_intervals": [
                {"start_date": s.isoformat(), "end_date": e.isoformat()}
                for s, e in enabled_pairs
            ],
            "suspended_intervals": [
                {"start_date": s.isoformat(), "end_date": e.isoformat()}
                for s, e in suspended_pairs
            ],
        })

    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "date_range": {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        "stations": canonical_stations,
    }


def _merge_intervals(
    intervals: list[tuple[date, date]],
) -> list[tuple[date, date]]:
    """Sort and merge touching/overlapping intervals (without clipping)."""
    ordered = sorted(intervals)
    merged: list[tuple[date, date]] = []
    for left, right in ordered:
        if merged and left <= merged[-1][1] + timedelta(days=1):
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _effective_days(
    intervals: list[tuple[date, date]], start: date, end: date
) -> set[date]:
    """All days covered by ``intervals`` inside the inclusive range."""
    days: set[date] = set()
    for left, right in intervals:
        left = max(left, start)
        right = min(right, end)
        if left > right:
            continue
        for day in iter_dates(left, right):
            days.add(day)
    return days


def iter_dates(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def required_days_by_station(arrangement: dict[str, Any]) -> dict[str, list[str]]:
    """Canonical arrangement -> required station-day strings."""
    start = parse_date_value(arrangement["start_date"])
    end = parse_date_value(arrangement["end_date"])
    result: dict[str, list[str]] = {}
    for station in arrangement["stations"]:
        enabled = _effective_days(
            [(parse_date_value(i["start_date"]), parse_date_value(i["end_date"]))
             for i in station["enabled_intervals"]],
            start, end,
        )
        suspended = _effective_days(
            [(parse_date_value(i["start_date"]), parse_date_value(i["end_date"]))
             for i in station["suspended_intervals"]],
            start, end,
        )
        result[station["station_id"]] = [
            day.isoformat() for day in sorted(enabled - suspended)
        ]
    return result


# ---------------------------------------------------------------------------
# Snapshot canonicalization and result computation
# ---------------------------------------------------------------------------

def snapshot_to_json(snapshot: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "station_id": station_id,
            "data_date": data_date,
            "version": value["version"],
            "file_id": value["file_id"],
        }
        for (station_id, data_date), value in sorted(snapshot.items())
    ]


def snapshot_from_json(raw: Any) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in raw or []:
        result[(item["station_id"], item["data_date"])] = {
            "version": int(item["version"]),
            "file_id": item["file_id"],
        }
    return result


def preview_token(arrangement: dict[str, Any],
                  snapshot: dict[tuple[str, str], dict[str, Any]]) -> str:
    """Deterministic token binding an arrangement to a publication snapshot."""
    import hashlib

    material = json.dumps(
        {
            "arrangement": arrangement,
            "publication_snapshot": snapshot_to_json(snapshot),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def arrangement_hash(arrangement: dict[str, Any]) -> str:
    """Stable hash of a canonical arrangement."""
    import hashlib

    material = json.dumps(
        arrangement, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def compute_result(
    arrangement: dict[str, Any],
    snapshot: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Compute the coverage result for ``arrangement`` and ``snapshot``."""
    start = parse_date_value(arrangement["start_date"])
    end = parse_date_value(arrangement["end_date"])
    day_count = (end - start).days + 1

    station_entries: list[dict[str, Any]] = []
    all_items: list[dict[str, Any]] = []
    all_missing: list[dict[str, Any]] = []
    all_published: list[dict[str, Any]] = []

    for station in arrangement["stations"]:
        station_id = station["station_id"]
        enabled = _effective_days(
            [(parse_date_value(i["start_date"]), parse_date_value(i["end_date"]))
             for i in station["enabled_intervals"]],
            start, end,
        )
        suspended = _effective_days(
            [(parse_date_value(i["start_date"]), parse_date_value(i["end_date"]))
             for i in station["suspended_intervals"]],
            start, end,
        )

        required = sorted(enabled - suspended)
        required_strings = [day.isoformat() for day in required]

        station_items: list[dict[str, Any]] = []
        station_missing: list[dict[str, Any]] = []
        station_published: list[dict[str, Any]] = []

        for day in required:
            date_str = day.isoformat()
            publication = snapshot.get((station_id, date_str))
            if publication is None:
                item = {
                    "station_id": station_id,
                    "date": date_str,
                    "data_date": date_str,
                    "status": "missing",
                    "reported": False,
                    "is_missing": True,
                    "version": None,
                    "file_id": None,
                }
                station_missing.append(item)
                all_missing.append(item)
            else:
                item = {
                    "station_id": station_id,
                    "date": date_str,
                    "data_date": date_str,
                    "status": "published",
                    "reported": True,
                    "is_missing": False,
                    "version": publication["version"],
                    "file_id": publication["file_id"],
                    "current_version": publication["version"],
                    "current_file_id": publication["file_id"],
                }
                station_published.append(item)
                all_published.append(item)
            station_items.append(item)
            all_items.append(item)

        station_entries.append({
            "station_id": station_id,
            "enabled_intervals": station["enabled_intervals"],
            "suspended_intervals": station["suspended_intervals"],
            "enabled_days": sorted(day.isoformat() for day in enabled),
            "suspended_days": sorted(day.isoformat() for day in suspended),
            "required_days": required_strings,
            "required_count": len(required),
            "published_days": station_published,
            "published": station_published,
            "published_count": len(station_published),
            "missing_days": [item["date"] for item in station_missing],
            "missing": station_missing,
            "missing_count": len(station_missing),
            "items": station_items,
        })

    return {
        "start_date": arrangement["start_date"],
        "end_date": arrangement["end_date"],
        "date_range": arrangement["date_range"],
        "day_count": day_count,
        "station_count": len(station_entries),
        "stations": station_entries,
        "items": all_items,
        "days": all_items,
        "missing": all_missing,
        "missing_reports": all_missing,
        "missing_station_days": all_missing,
        "missing_count": len(all_missing),
        "published": all_published,
        "published_count": len(all_published),
        "required_count": len(all_items),
        "required_station_days": all_items,
    }

"""SQLite persistence and optimistic version control.

Concurrency model
-----------------
Every publish opens one connection with ``BEGIN IMMEDIATE``, so competing
backfills are serialized at the database level. The current version is
checked *inside* that write transaction: only the request whose expected
version matches can commit, every concurrent loser gets a conflict error and
the winner's whole batch replaces the previous one.

Durability model
----------------
The immutable CSV of each version is written (via a temp file + atomic
``os.replace``) *before* the database transaction starts. If the transaction
fails or the version check conflicts, the previous version row still points at
the previous file, which is never overwritten or deleted; nothing already
published is ever lost or partially visible.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from . import config
from .validation import ParsedBatch

SCHEMA = """
CREATE TABLE IF NOT EXISTS upload_versions (
    station_id     TEXT NOT NULL,
    data_date      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    file_id        TEXT NOT NULL,
    file_path      TEXT NOT NULL,
    checksum       TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (station_id, data_date, version),
    UNIQUE (file_id)
);

CREATE TABLE IF NOT EXISTS hourly_records (
    station_id     TEXT NOT NULL,
    data_date      TEXT NOT NULL,
    version        INTEGER NOT NULL,
    hour           INTEGER NOT NULL CHECK (hour BETWEEN 0 AND 23),
    pm25           REAL NOT NULL CHECK (pm25 >= 0.0),
    PRIMARY KEY (station_id, data_date, version, hour),
    FOREIGN KEY (station_id, data_date, version)
        REFERENCES upload_versions (station_id, data_date, version)
);

CREATE INDEX IF NOT EXISTS idx_versions_lookup
    ON upload_versions (station_id, data_date, version);

CREATE TABLE IF NOT EXISTS coverage_previews (
    preview_id       TEXT PRIMARY KEY,
    preview_token    TEXT NOT NULL,
    arrangement_hash TEXT NOT NULL,
    arrangement_json TEXT NOT NULL,
    snapshot_json    TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coverage_previews_token
    ON coverage_previews (preview_token, created_at);

CREATE INDEX IF NOT EXISTS idx_coverage_previews_hash
    ON coverage_previews (arrangement_hash, created_at);

CREATE TABLE IF NOT EXISTS coverage_reports (
    report_id        TEXT PRIMARY KEY,
    preview_id       TEXT,
    arrangement_hash TEXT NOT NULL,
    arrangement_json TEXT NOT NULL,
    result_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coverage_reports_created
    ON coverage_reports (created_at);
"""


class VersionConflictError(Exception):
    """The supplied expected version no longer matches the current version."""

    def __init__(self, expected: int, current: int | None):
        self.expected = expected
        self.current = current
        super().__init__(
            f"version conflict: expected {expected}, current is {current}"
        )


class PublishError(Exception):
    """A storage or database failure during publish; old version stays intact."""


class CoverageConflictError(Exception):
    """A confirmed preview no longer matches the publication state.

    Raised when any station-day required by the previewed arrangement has been
    published or backfilled after the preview.  Nothing is saved in that case.
    """

    def __init__(self, changed: list[dict[str, Any]]):
        self.changed = changed
        self.changed_station_days = changed
        super().__init__("publication state changed after preview")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.DB_PATH, timeout=30.0, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    # Wake lock waiters periodically instead of deadlocking, and enforce
    # foreign keys on every connection.
    conn.execute(f"PRAGMA busy_timeout = {30_000}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db() -> None:
    config.ensure_dirs()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_current_version(
    conn: sqlite3.Connection, station_id: str, data_date: str
) -> int | None:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM upload_versions "
        "WHERE station_id = ? AND data_date = ?",
        (station_id, data_date),
    ).fetchone()
    return None if row is None or row["v"] is None else int(row["v"])


def _version_row(
    conn: sqlite3.Connection, station_id: str, data_date: str, version: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM upload_versions "
        "WHERE station_id = ? AND data_date = ? AND version = ?",
        (station_id, data_date, version),
    ).fetchone()


def persist_csv_file(
    station_id: str, data_date: str, version: int, batch: ParsedBatch
) -> tuple[str, str]:
    """Write the raw CSV to an immutable path atomically.

    Returns ``(file_id, relative_path)``. Raises :class:`PublishError` on any
    filesystem failure; no previously published file is touched.
    """
    file_id = uuid.uuid4().hex
    # Immutable per-version path: backfills never overwrite a stored CSV.
    rel_path = Path(station_id) / data_date / f"v{version}_{file_id}.csv"
    final_path = config.FILES_DIR / rel_path
    tmp_path = config.TMP_DIR / f".{file_id}.tmp.csv"
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "wb") as fh:
            fh.write(batch.raw_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)
    except OSError as exc:
        # Best-effort cleanup of the temp file; the published tree is untouched.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise PublishError(f"failed to store CSV file: {exc}") from exc
    return file_id, str(rel_path)


def _insert_records(conn, station_id: str, date_str: str,
                    version: int, rows: list[tuple[int, float]]) -> None:
    """Indirection point so failure tests can simulate a database write error."""
    conn.executemany(
        "INSERT INTO hourly_records "
        "(station_id, data_date, version, hour, pm25) "
        "VALUES (?, ?, ?, ?, ?)",
        [(station_id, date_str, version, hour, pm25) for hour, pm25 in rows],
    )


def publish_batch(
    station_id: str,
    data_date: date,
    expected_version: int,
    batch: ParsedBatch,
) -> dict[str, Any]:
    """Validate optimistic version and commit a full batch replacement.

    Runs in a single ``BEGIN IMMEDIATE`` transaction so concurrent backfills
    serialize and exactly one wins.
    """
    date_str = data_date.isoformat()
    conn = connect()
    try:
        # Acquire the write lock up front, serializing concurrent publishes.
        conn.execute("BEGIN IMMEDIATE")

        current = get_current_version(conn, station_id, date_str)
        if expected_version != (current or 0):
            conn.execute("ROLLBACK")
            raise VersionConflictError(expected=expected_version, current=current)

        new_version = (current or 0) + 1

        # File first; if this fails, no transaction content exists yet and the
        # previous version remains fully queryable. Referenced via module
        # attribute so the storage layer can be swapped in failure tests.
        try:
            file_id, rel_path = sys.modules[__name__].persist_csv_file(
                station_id, date_str, new_version, batch
            )
        except (PublishError, OSError) as exc:
            conn.execute("ROLLBACK")
            if isinstance(exc, PublishError):
                raise
            raise PublishError(f"failed to store CSV file: {exc}") from exc

        try:
            conn.execute(
                "INSERT INTO upload_versions "
                "(station_id, data_date, version, file_id, file_path, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    station_id,
                    date_str,
                    new_version,
                    file_id,
                    rel_path,
                    batch.checksum,
                ),
            )
            sys.modules[__name__]._insert_records(
                conn, station_id, date_str, new_version, batch.rows
            )
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            # The orphaned new file is harmless (nothing references it), but
            # remove it to keep the storage tree tidy.
            try:
                (config.FILES_DIR / rel_path).unlink(missing_ok=True)
            except OSError:
                pass
            raise PublishError(f"database write failed: {exc}") from exc

        conn.execute("COMMIT")
    except VersionConflictError:
        raise
    except PublishError:
        raise
    except sqlite3.Error as exc:
        # Lock/timeout or any unexpected SQLite failure: old version survives.
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise PublishError(f"database transaction failed: {exc}") from exc
    finally:
        conn.close()

    return {
        "station_id": station_id,
        "data_date": date_str,
        "version": new_version,
        "file_id": file_id,
        "checksum": batch.checksum,
    }


def get_current_data(
    station_id: str, data_date: date
) -> dict[str, Any] | None:
    """Return the current version's metadata and 24 hourly records."""
    date_str = data_date.isoformat()
    conn = connect()
    try:
        version = get_current_version(conn, station_id, date_str)
        if version is None:
            return None
        meta = _version_row(conn, station_id, date_str, version)
        records = conn.execute(
            "SELECT hour, pm25 FROM hourly_records "
            "WHERE station_id = ? AND data_date = ? AND version = ? "
            "ORDER BY hour",
            (station_id, date_str, version),
        ).fetchall()
        return {
            "station_id": station_id,
            "data_date": date_str,
            "version": version,
            "file_id": meta["file_id"],
            "file_path": meta["file_path"],
            "checksum": meta["checksum"],
            "created_at": meta["created_at"],
            "records": [
                {"hour": row["hour"], "pm25": row["pm25"]} for row in records
            ],
        }
    finally:
        conn.close()


def list_versions(station_id: str, data_date: date) -> list[dict[str, Any]]:
    """Return every published version (newest first), including file ids."""
    date_str = data_date.isoformat()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT version, file_id, file_path, checksum, created_at "
            "FROM upload_versions "
            "WHERE station_id = ? AND data_date = ? ORDER BY version DESC",
            (station_id, date_str),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_version_file(
    station_id: str, data_date: date, version: int
) -> dict[str, Any] | None:
    date_str = data_date.isoformat()
    conn = connect()
    try:
        row = _version_row(conn, station_id, date_str, version)
        if row is None:
            return None
        return dict(row)
    finally:
        conn.close()


def reset_for_tests() -> None:
    """Drop all data; used by the test suite against a temp data dir."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(
            "DELETE FROM hourly_records; DELETE FROM upload_versions;"
            "DELETE FROM coverage_previews; DELETE FROM coverage_reports;"
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Coverage reports
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def read_publication_snapshot(
    pairs: list[tuple[str, str]],
    conn: sqlite3.Connection | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return the current version/file id for each requested station-day.

    Only the requested ``(station_id, data_date)`` pairs are read, so backfills
    of unrelated station-days cannot affect a coverage report.  A single
    SELECT gives a consistent snapshot of the publication state.
    """
    if not pairs:
        return {}

    wanted = set(pairs)
    stations = sorted({station for station, _ in wanted})
    all_dates = sorted({day for _, day in wanted})
    owns_conn = conn is None
    if conn is None:
        conn = connect()
    try:
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        # Chunk station ids to stay below SQLite's bound-parameter comfort zone.
        for offset in range(0, len(stations), 500):
            chunk = stations[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "SELECT station_id, data_date, version, file_id "
                "FROM upload_versions "
                f"WHERE station_id IN ({placeholders}) "
                "AND data_date >= ? AND data_date <= ? "
                "ORDER BY station_id, data_date, version",
                (*chunk, all_dates[0], all_dates[-1]),
            ).fetchall()
            for row in rows:
                key = (row["station_id"], row["data_date"])
                if key not in wanted:
                    continue
                latest[key] = {
                    "version": int(row["version"]),
                    "file_id": row["file_id"],
                }
        return latest
    finally:
        if owns_conn:
            conn.close()


def save_coverage_preview(
    arrangement: dict[str, Any],
    snapshot: dict[tuple[str, str], dict[str, Any]],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Persist a preview, including the exact arrangement and snapshot."""
    import hashlib

    from . import coverage

    preview_id = uuid.uuid4().hex
    token = coverage.preview_token(arrangement, snapshot)
    arrangement_json = json.dumps(
        arrangement, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    arrangement_hash = hashlib.sha256(arrangement_json.encode("utf-8")).hexdigest()
    created_at = _now_iso()

    conn = connect()
    try:
        conn.execute(
            "INSERT INTO coverage_previews "
            "(preview_id, preview_token, arrangement_hash, arrangement_json, "
            " snapshot_json, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                preview_id,
                token,
                arrangement_hash,
                arrangement_json,
                json.dumps(coverage.snapshot_to_json(snapshot),
                           ensure_ascii=False, separators=(",", ":")),
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )
    except sqlite3.Error as exc:
        raise PublishError(f"failed to save coverage preview: {exc}") from exc
    finally:
        conn.close()

    return {
        "preview_id": preview_id,
        "preview_token": token,
        "created_at": created_at,
        "arrangement_hash": arrangement_hash,
    }


def get_coverage_preview(preview_id: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM coverage_previews WHERE preview_id = ?",
            (preview_id,),
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def get_coverage_preview_by_token(token: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM coverage_previews WHERE preview_token = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (token,),
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def find_latest_coverage_preview(arrangement_hash: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM coverage_previews WHERE arrangement_hash = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (arrangement_hash,),
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def _changed_station_days(
    expected: dict[tuple[str, str], dict[str, Any]],
    current: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    changed: list[dict[str, Any]] = []
    for key in sorted(set(expected) | set(current)):
        before = expected.get(key)
        after = current.get(key)
        if before == after:
            continue
        changed.append({
            "station_id": key[0],
            "data_date": key[1],
            "date": key[1],
            "preview_version": None if before is None else before["version"],
            "preview_file_id": None if before is None else before["file_id"],
            "current_version": None if after is None else after["version"],
            "current_file_id": None if after is None else after["file_id"],
        })
    return changed


def create_coverage_report(
    arrangement: dict[str, Any],
    required_pairs: list[tuple[str, str]],
    expected_snapshot: dict[tuple[str, str], dict[str, Any]] | None,
    preview_id: str | None = None,
) -> dict[str, Any]:
    """Atomically verify the preview and save a confirmed report.

    ``BEGIN IMMEDIATE`` serializes confirmations against backfills.  The
    publication snapshot is read *inside* the write transaction, then compared
    with the previewed snapshot.  If any relevant station-day changed, the
    transaction is rolled back and no report row is left behind.
    """
    import hashlib

    from . import coverage

    arrangement_json = json.dumps(
        arrangement, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    arrangement_hash = hashlib.sha256(arrangement_json.encode("utf-8")).hexdigest()

    conn = connect()
    committed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_snapshot = read_publication_snapshot(required_pairs, conn=conn)

        if expected_snapshot is not None and expected_snapshot != current_snapshot:
            changed = _changed_station_days(expected_snapshot, current_snapshot)
            conn.execute("ROLLBACK")
            raise CoverageConflictError(changed)

        result = coverage.compute_result(arrangement, current_snapshot)
        report_id = uuid.uuid4().hex
        created_at = _now_iso()
        conn.execute(
            "INSERT INTO coverage_reports "
            "(report_id, preview_id, arrangement_hash, arrangement_json, "
            " result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                report_id,
                preview_id,
                arrangement_hash,
                arrangement_json,
                json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )
        conn.execute("COMMIT")
        committed = True
    except CoverageConflictError:
        raise
    except sqlite3.Error as exc:
        if not committed:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise PublishError(f"failed to save coverage report: {exc}") from exc
    finally:
        conn.close()

    return {
        "report_id": report_id,
        "preview_id": preview_id,
        "created_at": created_at,
        "arrangement_hash": arrangement_hash,
        "result": result,
    }


def get_coverage_report(report_id: str) -> dict[str, Any] | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM coverage_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def list_coverage_reports(limit: int = 100,
                          offset: int = 0) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM coverage_reports "
            "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# Kept here so callers can render structured failure details uniformly.
def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)

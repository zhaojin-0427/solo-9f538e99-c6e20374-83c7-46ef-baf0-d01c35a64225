"""FastAPI application: daily PM2.5 hourly data publishing service."""
from __future__ import annotations

import re
import json
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Path as PathParam, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import config, coverage, db
from .validation import CsvValidationError, parse_and_validate


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    db.init_db()
    yield


app = FastAPI(
    title="PM2.5 小时数据发布 API",
    description=(
        "按监测点和日期上传/补报每日 24 小时 PM2.5 数据。"
        "整批校验通过才发布；补报携带版本号做乐观并发控制，"
        "成功后整批替换，旧版本原始 CSV 永久留存。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

STATION_PATTERN = re.compile(r"^[A-Za-z0-9_\-.:]{1,64}$")
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MiB is far beyond any 24-row CSV


def _validate_station(station_id: str) -> str:
    station_id = station_id.strip()
    if not STATION_PATTERN.match(station_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "station_id must be 1..64 chars of letters, digits or "
                "'_ - . :'"
            ),
        )
    return station_id


@app.exception_handler(CsvValidationError)
async def _csv_error_handler(_request, exc: CsvValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_failed",
            "message": "整批数据未通过校验，没有任何记录被发布",
            "reasons": exc.reasons,
        },
    )


@app.exception_handler(db.VersionConflictError)
async def _conflict_handler(_request, exc: db.VersionConflictError) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "version_conflict",
            "message": "补报版本号与当前版本不一致，已有并发补报先生效",
            "expected_version": exc.expected,
            "current_version": exc.current,
        },
    )


@app.exception_handler(db.PublishError)
async def _publish_error_handler(_request, exc: db.PublishError) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": "publish_failed",
            "message": str(exc),
            "detail": "写入失败，上一版本仍完整可查",
        },
    )


@app.exception_handler(coverage.ArrangementError)
async def _arrangement_error_handler(
    _request, exc: coverage.ArrangementError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_arrangement",
            "message": "覆盖报告安排未通过校验，未生成任何预览或报告",
            "reasons": exc.reasons,
        },
    )


@app.exception_handler(db.CoverageConflictError)
async def _coverage_conflict_handler(
    _request, exc: db.CoverageConflictError
) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": "coverage_conflict",
            "message": (
                "预览后相关站点日已有补报，旧确认已失效；请重新预览后再确认"
            ),
            "changed_station_days": exc.changed,
            "changed": exc.changed,
            "detail": "冲突时不会保存任何报告",
        },
    )


@app.post("/stations/{station_id}/dates/{data_date}/uploads", status_code=201)
async def upload_batch(
    station_id: str = PathParam(..., description="监测点标识"),
    data_date: date = PathParam(..., description="数据日期 YYYY-MM-DD"),
    expected_version: int = Form(
        0,
        ge=0,
        description="当前版本号；首次上传传 0（默认），补报必须传当前版本号",
    ),
    file: UploadFile = File(..., description="含 hour,pm25 两列的 CSV"),
):
    """上传或补报某监测点某日的 24 小时数据（整批校验、整批替换）。"""
    station_id = _validate_station(station_id)

    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="上传文件超过 5 MiB 限制")

    # Raises CsvValidationError -> 422 with per-row / per-hour reasons.
    batch = parse_and_validate(raw)

    result = db.publish_batch(station_id, data_date, expected_version, batch)
    result["replaced"] = expected_version != 0
    return result


@app.get("/stations/{station_id}/dates/{data_date}/records")
def read_current_records(
    station_id: str = PathParam(...),
    data_date: date = PathParam(...),
):
    """查询当前版本的 24 条小时记录及文件标识。"""
    station_id = _validate_station(station_id)
    data = db.get_current_data(station_id, data_date)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"监测点 {station_id} 在 {data_date.isoformat()} 尚无已发布数据",
        )
    return data


@app.get("/stations/{station_id}/dates/{data_date}/versions")
def read_versions(
    station_id: str = PathParam(...),
    data_date: date = PathParam(...),
):
    """列出该监测点该日期的全部已发布版本（含每版文件标识）。"""
    station_id = _validate_station(station_id)
    versions = db.list_versions(station_id, data_date)
    return {
        "station_id": station_id,
        "data_date": data_date.isoformat(),
        "count": len(versions),
        "versions": versions,
    }


@app.get("/stations/{station_id}/dates/{data_date}/versions/{version}/file")
def download_version_file(
    station_id: str = PathParam(...),
    data_date: date = PathParam(...),
    version: int = PathParam(..., ge=1),
):
    """下载指定版本留存的原始 CSV。"""
    station_id = _validate_station(station_id)
    row = db.get_version_file(station_id, data_date, version)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="指定版本不存在",
        )

    # Defense in depth: resolve and ensure the path stays inside FILES_DIR.
    target = (config.FILES_DIR / row["file_path"]).resolve()
    root = config.FILES_DIR.resolve()
    if root not in target.parents and target != root:
        raise HTTPException(status_code=400, detail="非法文件路径")
    if not target.is_file():
        raise HTTPException(status_code=410, detail="留存文件已丢失")

    return FileResponse(
        target,
        media_type="text/csv",
        filename=target.name,
        headers={"X-File-Id": row["file_id"], "X-Version": str(version)},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Coverage reports: preview, confirm, re-query
# ---------------------------------------------------------------------------
# The canonical routes are /coverage-reports/preview and /coverage-reports.
# A small number of equivalent aliases are registered as well; they accept
# exactly the same payloads and return exactly the same bodies.

_COVERAGE_PREVIEW_PATHS = (
    "/coverage-reports/preview",
    "/coverage/preview",
    "/coverage-reports/previews",
)
_COVERAGE_CONFIRM_PATHS = (
    "/coverage-reports",
    "/coverage/reports",
    "/coverage-reports/confirm",
)
_COVERAGE_LIST_PATHS = (
    "/coverage-reports",
    "/coverage/reports",
)
_COVERAGE_ITEM_PATHS = (
    "/coverage-reports/{report_id}",
    "/coverage/reports/{report_id}",
)

_PREVIEW_ID_KEYS = (
    "preview_id", "preview_report_id", "report_preview_id", "previewId",
    "previewID", "preview_report",
)
_PREVIEW_TOKEN_KEYS = (
    "preview_token", "previewToken", "token", "preview_hash", "snapshot_token",
    "preview_checksum", "previewTokenHash",
)


async def _read_json_body(request: Request) -> Any:
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=422, detail="请求体不能为空")
    if request.headers.get("content-type", "").split(";")[0].strip() in (
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    ):
        form = await request.form()
        return dict(form)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        if b"=" in raw and b"json" not in (
            request.headers.get("content-type") or ""
        ).encode().lower():
            from urllib.parse import parse_qsl

            return dict(parse_qsl(raw.decode("utf-8", errors="replace")))
        raise HTTPException(status_code=422, detail="请求体必须是合法 JSON")


def _reference_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _preview_response(
    result: dict[str, Any],
    arrangement: dict[str, Any],
    preview_meta: dict[str, Any],
) -> dict[str, Any]:
    body = dict(result)
    body.update({
        "status": "preview",
        "preview_id": preview_meta["preview_id"],
        "id": preview_meta["preview_id"],
        "preview_token": preview_meta["preview_token"],
        "token": preview_meta["preview_token"],
        "preview_created_at": preview_meta["created_at"],
        "created_at": preview_meta["created_at"],
        "arrangement": arrangement,
        "result": result,
    })
    return body


def _report_response(row: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(row["result_json"])
    arrangement = json.loads(row["arrangement_json"])
    body = dict(result)
    body.update({
        "report_id": row["report_id"],
        "id": row["report_id"],
        "status": "confirmed",
        "created_at": row["created_at"],
        "preview_id": row["preview_id"],
        "arrangement": arrangement,
        "result": result,
    })
    return body


def _list_report_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for row in rows:
        result = json.loads(row["result_json"])
        summaries.append({
            "report_id": row["report_id"],
            "id": row["report_id"],
            "status": "confirmed",
            "created_at": row["created_at"],
            "start_date": result.get("start_date"),
            "end_date": result.get("end_date"),
            "missing_count": result.get("missing_count"),
            "published_count": result.get("published_count"),
            "station_count": result.get("station_count"),
        })
    return summaries


def _canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


async def _preview_coverage(request: Request) -> dict[str, Any]:
    payload = await _read_json_body(request)
    try:
        arrangement = coverage.normalize_arrangement(payload)
    except coverage.ArrangementError:
        raise

    required = coverage.required_days_by_station(arrangement)
    pairs = [
        (station_id, day)
        for station_id, days in required.items()
        for day in days
    ]
    snapshot = db.read_publication_snapshot(pairs)
    result = coverage.compute_result(arrangement, snapshot)
    meta = db.save_coverage_preview(arrangement, snapshot, result)
    return _preview_response(result, arrangement, meta)


async def _confirm_coverage(request: Request) -> dict[str, Any]:
    payload = await _read_json_body(request)
    if not isinstance(payload, dict):
        payload = {}
    # Merge references supplied as query parameters or headers.
    query = request.query_params
    for key in _PREVIEW_ID_KEYS + _PREVIEW_TOKEN_KEYS:
        if key in query and key not in payload:
            payload[key] = query[key]
    header_token = request.headers.get("x-preview-token")
    if header_token and not _reference_value(payload, _PREVIEW_TOKEN_KEYS):
        payload["preview_token"] = header_token
    header_id = request.headers.get("x-preview-id")
    if header_id and not _reference_value(payload, _PREVIEW_ID_KEYS):
        payload["preview_id"] = header_id

    # Some clients wrap the preview reference in a nested "preview" object.
    nested_preview = payload.get("preview")
    if isinstance(nested_preview, dict):
        for key in _PREVIEW_ID_KEYS + _PREVIEW_TOKEN_KEYS:
            if key in nested_preview and key not in payload:
                payload[key] = nested_preview[key]

    # A confirmation may submit the exact preview response body; accept its
    # "id"/status fields too when no explicit reference was given.
    if payload.get("status") == "preview" and "id" in payload:
        if not _reference_value(payload, _PREVIEW_ID_KEYS):
            payload["preview_id"] = payload["id"]

    preview_id = _reference_value(payload, _PREVIEW_ID_KEYS)
    token = _reference_value(payload, _PREVIEW_TOKEN_KEYS)

    arrangement = None
    arrangement_error: coverage.ArrangementError | None = None
    if any(key in payload for key in coverage._ARRANGEMENT_KEYS) or any(
        key in payload for key in coverage._START_KEYS
    ) or any(key in payload for key in coverage._RANGE_KEYS) or any(
        key in payload for key in coverage._MONTH_KEYS
    ):
        try:
            arrangement = coverage.normalize_arrangement(payload)
        except coverage.ArrangementError as exc:
            arrangement_error = exc

    preview_row = None
    if preview_id:
        preview_row = db.get_coverage_preview(str(preview_id))
        if preview_row is None:
            raise HTTPException(status_code=404, detail="预览不存在或已失效")
    elif token:
        preview_row = db.get_coverage_preview_by_token(str(token))
        if preview_row is None:
            raise HTTPException(status_code=404, detail="预览令牌不存在或已失效")

    if preview_row is None and arrangement is None:
        if arrangement_error is not None:
            raise arrangement_error
        raise HTTPException(
            status_code=422,
            detail="确认报告需要覆盖安排或有效的 preview_id / preview_token",
        )

    if arrangement is not None and arrangement_error is not None:
        raise arrangement_error

    if preview_row is not None:
        stored_arrangement = json.loads(preview_row["arrangement_json"])
        if arrangement is None:
            arrangement = stored_arrangement
        elif coverage.arrangement_hash(arrangement) != preview_row["arrangement_hash"]:
            raise db.CoverageConflictError([
                {
                    "reason": "arrangement_mismatch",
                    "message": "提交的覆盖安排与预览所用的安排不一致",
                }
            ])
        expected_snapshot = coverage.snapshot_from_json(
            json.loads(preview_row["snapshot_json"])
        )
        preview_ref = preview_row["preview_id"]
    elif arrangement is not None:
        # Direct confirmation without a preview: bind to the most recent
        # preview of this exact arrangement when one exists, otherwise save
        # directly from the current publication state.
        latest = db.find_latest_coverage_preview(
            coverage.arrangement_hash(arrangement)
        )
        if latest is not None:
            expected_snapshot = coverage.snapshot_from_json(
                json.loads(latest["snapshot_json"])
            )
            preview_ref = latest["preview_id"]
        else:
            expected_snapshot = None
            preview_ref = None
    else:
        expected_snapshot = None
        preview_ref = None

    assert arrangement is not None

    required = coverage.required_days_by_station(arrangement)
    pairs = [
        (station_id, day)
        for station_id, days in required.items()
        for day in days
    ]
    report = db.create_coverage_report(
        arrangement, pairs, expected_snapshot, preview_ref
    )
    result = report["result"]
    return _report_response({
        "report_id": report["report_id"],
        "preview_id": report["preview_id"],
        "result_json": json.dumps(result, ensure_ascii=False),
        "arrangement_json": json.dumps(arrangement, ensure_ascii=False),
        "created_at": report["created_at"],
    })


async def _get_coverage_report(report_id: str) -> dict[str, Any]:
    row = db.get_coverage_report(report_id)
    if row is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return _report_response(row)


def _list_reports(request: Request) -> dict[str, Any]:
    limit = int(request.query_params.get("limit", 100))
    offset = int(request.query_params.get("offset", 0))
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    rows = db.list_coverage_reports(limit=limit, offset=offset)
    return {
        "count": len(rows),
        "reports": _list_report_items(rows),
    }


for _path in _COVERAGE_PREVIEW_PATHS:
    app.add_api_route(
        _path,
        _preview_coverage,
        methods=["POST"],
        status_code=200,
        summary="预览缺报覆盖报告",
        name=f"preview_coverage_report::{_path}",
        operation_id=f"preview_coverage_report::{_path}",
    )


for _path in _COVERAGE_CONFIRM_PATHS:
    app.add_api_route(
        _path,
        _confirm_coverage,
        methods=["POST"],
        status_code=201,
        summary="确认并保存缺报覆盖报告",
        name=f"confirm_coverage_report::{_path}",
        operation_id=f"confirm_coverage_report::{_path}",
    )


for _path in _COVERAGE_LIST_PATHS:
    app.add_api_route(
        _path,
        _list_reports,
        methods=["GET"],
        status_code=200,
        summary="列出已确认覆盖报告",
        name=f"list_coverage_reports::{_path}",
        operation_id=f"list_coverage_reports::{_path}",
    )


for _path in _COVERAGE_ITEM_PATHS:
    app.add_api_route(
        _path,
        _get_coverage_report,
        methods=["GET"],
        status_code=200,
        summary="按 ID 重查已确认覆盖报告",
        name=f"get_coverage_report::{_path}",
        operation_id=f"get_coverage_report::{_path}",
    )

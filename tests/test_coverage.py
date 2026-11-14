"""Coverage report preview/confirm tests: participation, deduction, conflicts."""
from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

from conftest import full_csv


def _upload(client, station, day, expected_version=0):
    return client.post(
        f"/stations/{station}/dates/{day}/uploads",
        data={"expected_version": str(expected_version)},
        files={"file": ("data.csv", io.BytesIO(full_csv()), "text/csv")},
    )


def _arrangement():
    return {
        "start_date": "2026-11-01",
        "end_date": "2026-11-10",
        "stations": [
            {
                "station_id": "ST-A",
                # enabled 1..10, suspended 3..5 -> required 1,2,6..10
                "enabled_intervals": [["2026-11-01", "2026-11-10"]],
                "suspended_intervals": [["2026-11-03", "2026-11-05"]],
            },
            {
                "station_id": "ST-B",
                # enabled only 2..4; suspended includes day 7 which is not
                # enabled and must not influence anything.
                "enabled_intervals": [["2026-11-02", "2026-11-04"]],
                "suspended_intervals": [["2026-11-07", "2026-11-07"]],
            },
        ],
    }


def test_preview_lists_missing_and_published_with_version_and_file_id(client):
    _upload(client, "ST-A", "2026-11-02")
    _upload(client, "ST-A", "2026-11-06")
    _upload(client, "ST-B", "2026-11-04")

    resp = client.post("/coverage-reports/preview", json=_arrangement())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "preview"
    assert body["preview_id"] and body["preview_token"]
    assert body["start_date"] == "2026-11-01"
    assert body["end_date"] == "2026-11-10"

    missing = {(m["station_id"], m["data_date"]) for m in body["missing"]}
    # ST-A: all enabled days except the suspension and the two published
    assert missing == {
        ("ST-A", "2026-11-01"), ("ST-A", "2026-11-07"),
        ("ST-A", "2026-11-08"), ("ST-A", "2026-11-09"),
        ("ST-A", "2026-11-10"),
        ("ST-B", "2026-11-02"), ("ST-B", "2026-11-03"),
    }
    assert body["missing_count"] == 7
    assert ("ST-A", "2026-11-03") not in missing
    assert ("ST-A", "2026-11-04") not in missing
    assert ("ST-A", "2026-11-05") not in missing
    assert ("ST-B", "2026-11-07") not in missing  # suspended but not enabled

    published = {(p["station_id"], p["data_date"]): p
                 for p in body["published"]}
    assert set(published) == {
        ("ST-A", "2026-11-02"), ("ST-A", "2026-11-06"), ("ST-B", "2026-11-04")
    }
    for station_day, item in published.items():
        assert item["version"] == 1
        assert len(item["file_id"]) == 32
        assert item["current_version"] == 1
        assert item["current_file_id"] == item["file_id"]

    per_station = {s["station_id"]: s for s in body["stations"]}
    assert per_station["ST-A"]["missing_count"] == 5
    assert per_station["ST-B"]["missing_count"] == 2
    assert per_station["ST-A"]["required_count"] == 7
    assert per_station["ST-B"]["required_count"] == 3


def test_confirm_saves_and_get_returns_same_result(client):
    _upload(client, "ST-A", "2026-11-02")
    preview = client.post("/coverage-reports/preview", json=_arrangement()).json()

    created = client.post(
        "/coverage-reports",
        json={"preview_id": preview["preview_id"]},
    )
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["status"] == "confirmed"
    assert report["report_id"]
    assert report["missing"] == preview["missing"]
    assert report["arrangement"] == preview["arrangement"]

    fetched = client.get(f"/coverage-reports/{report['report_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["missing"] == preview["missing"]
    assert fetched.json()["arrangement"] == preview["arrangement"]

    # The saved report kept the previewed arrangement even if the request
    # omitted it on confirmation.
    assert fetched.json()["start_date"] == "2026-11-01"


def test_confirm_accepts_preview_token_and_omitted_enabled_default(client):
    preview = client.post(
        "/coverage-reports/preview",
        json={
            "start_date": "2026-11-01",
            "end_date": "2026-11-31" if False else "2026-11-03",
            "stations": [{"station_id": "ST-A"}],  # enabled defaults to all
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["missing_count"] == 3

    confirmed = client.post(
        "/coverage-reports/confirm",
        json={"preview_token": preview.json()["preview_token"]},
    )
    assert confirmed.status_code == 201, confirmed.text


def test_backfill_of_relevant_station_day_after_preview_conflicts(client):
    _upload(client, "ST-A", "2026-11-02")
    preview = client.post("/coverage-reports/preview", json=_arrangement()).json()

    # Backfill one of the days that was already published in the preview.
    assert _upload(client, "ST-A", "2026-11-02", expected_version=1).status_code == 201

    conflict = client.post(
        "/coverage-reports", json={"preview_id": preview["preview_id"]}
    )
    assert conflict.status_code == 409, conflict.text
    body = conflict.json()
    assert body["error"] == "coverage_conflict"
    assert any(
        c["station_id"] == "ST-A" and c["data_date"] == "2026-11-02"
        for c in body["changed_station_days"]
    )

    # No report was left behind; only a direct re-query can 404.
    reports = client.get("/coverage-reports").json()
    assert reports["count"] == 0


def test_backfill_of_newly_published_missing_day_after_preview_conflicts(client):
    preview = client.post("/coverage-reports/preview", json=_arrangement()).json()
    # A day that was missing in the preview is now published.
    assert _upload(client, "ST-B", "2026-11-02").status_code == 201

    conflict = client.post(
        "/coverage-reports", json={"preview_id": preview["preview_id"]}
    )
    assert conflict.status_code == 409
    assert any(
        c["station_id"] == "ST-B" and c["data_date"] == "2026-11-02"
        for c in conflict.json()["changed_station_days"]
    )
    assert client.get("/coverage-reports").json()["count"] == 0


def test_unrelated_station_day_backfill_does_not_affect_confirmation(client):
    _upload(client, "ST-A", "2026-11-02")
    preview = client.post("/coverage-reports/preview", json=_arrangement()).json()

    # Outside the requested range -> irrelevant.
    assert _upload(client, "ST-C", "2026-11-01").status_code == 201
    assert _upload(client, "ST-A", "2026-10-31").status_code == 201
    # Suspended day -> not required by the arrangement.
    assert _upload(client, "ST-A", "2026-11-03").status_code == 201
    # Backfill of a required published day with identical bytes still bumps
    # the version, so that one *is* relevant; use a missing unrelated station
    # day instead: ST-B day 8 is outside its enabled intervals.
    assert _upload(client, "ST-B", "2026-11-08").status_code == 201

    confirmed = client.post(
        "/coverage-reports", json={"preview_id": preview["preview_id"]}
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["missing"] == preview["missing"]


def test_confirmed_report_is_stable_after_later_backfills(client):
    preview = client.post("/coverage-reports/preview", json=_arrangement()).json()
    created = client.post(
        "/coverage-reports", json={"preview_id": preview["preview_id"]}
    )
    assert created.status_code == 201
    report = created.json()

    _upload(client, "ST-A", "2026-11-01")             # a missing day
    _upload(client, "ST-A", "2026-11-02", 1)          # a published day

    again = client.get(f"/coverage-reports/{report['report_id']}").json()
    assert again["missing"] == report["missing"]
    assert again["published"] == report["published"]
    assert again["result"]["missing"] == report["missing"]


def test_confirmation_and_backfill_concurrent_are_ordered(client):
    _upload(client, "ST-A", "2026-11-02")
    preview = client.post("/coverage-reports/preview", json=_arrangement()).json()

    outcomes = []
    lock = threading.Lock()

    def confirm():
        resp = client.post(
            "/coverage-reports", json={"preview_id": preview["preview_id"]}
        )
        with lock:
            outcomes.append(("confirm", resp.status_code))

    def backfill():
        resp = _upload(client, "ST-A", "2026-11-02", expected_version=1)
        with lock:
            outcomes.append(("backfill", resp.status_code))

    import random
    threads = []
    # Many racing pairs; whichever side commits first decides the outcome.
    order = [confirm, backfill] * 3
    random.shuffle(order)
    for fn in order:
        t = threading.Thread(target=fn)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    confirms = [status for name, status in outcomes if name == "confirm"]
    assert all(status in (201, 409) for status in confirms)
    reports = client.get("/coverage-reports").json()
    # Confirmation and backfill are serialized in submission order: a confirm
    # that commits before the backfill saves its report, one that runs after
    # it sees the changed relevant day and saves nothing.
    assert reports["count"] == confirms.count(201)
    for summary in reports["reports"]:
        full = client.get(f"/coverage-reports/{summary['report_id']}").json()
        assert full["status"] == "confirmed"
        assert full["missing"] is not None


def test_validation_limits_and_bad_intervals(client):
    too_long = client.post(
        "/coverage-reports/preview",
        json={
            "start_date": "2026-11-01",
            "end_date": "2026-12-02",  # 32 days
            "stations": [{"station_id": "ST-A"}],
        },
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"] == "invalid_arrangement"

    reversed_range = client.post(
        "/coverage-reports/preview",
        json={
            "start_date": "2026-11-10",
            "end_date": "2026-11-01",
            "stations": [{"station_id": "ST-A"}],
        },
    )
    assert reversed_range.status_code == 422

    bad_interval = client.post(
        "/coverage-reports/preview",
        json={
            "start_date": "2026-11-01",
            "end_date": "2026-11-03",
            "stations": [{
                "station_id": "ST-A",
                "suspended_intervals": [["2026-11-05", "2026-11-01"]],
            }],
        },
    )
    assert bad_interval.status_code == 422

    bad_station = client.post(
        "/coverage-reports/preview",
        json={
            "start_date": "2026-11-01",
            "end_date": "2026-11-03",
            "stations": [{"station_id": "bad station!"}],
        },
    )
    assert bad_station.status_code == 422


def test_max_31_day_range_is_accepted(client):
    resp = client.post(
        "/coverage-reports/preview",
        json={
            "start_date": "2026-11-01",
            "end_date": "2026-12-01",  # 31 days
            "stations": [{"station_id": "ST-A"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["missing_count"] == 31


def test_get_unknown_report_404(client):
    assert client.get("/coverage-reports/nope").status_code == 404


def test_confirm_with_unknown_preview_404(client):
    assert client.post(
        "/coverage-reports", json={"preview_id": "nope"}
    ).status_code == 404


def test_confirm_without_reference_or_arrangement_422(client):
    assert client.post("/coverage-reports", json={}).status_code == 422


def test_months_shortcut_and_chinese_interval_strings(client):
    resp = client.post(
        "/coverage-reports/preview",
        json={
            "month": "2026-11",
            "stations": [{
                "station_id": "ST-A",
                "enabled_intervals": ["2026-11-01至2026-11-03"],
            }],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["start_date"] == "2026-11-01"
    assert body["end_date"] == "2026-11-30"
    assert body["missing_count"] == 3


def test_confirm_with_arrangement_mismatching_preview_conflicts(client):
    preview = client.post("/coverage-reports/preview", json=_arrangement()).json()
    other = dict(_arrangement())
    other["end_date"] = "2026-11-09"

    resp = client.post(
        "/coverage-reports",
        json={"preview_id": preview["preview_id"], "arrangement": other},
    )
    assert resp.status_code == 409
    assert resp.json()["changed"][0]["reason"] == "arrangement_mismatch"
    assert client.get("/coverage-reports").json()["count"] == 0

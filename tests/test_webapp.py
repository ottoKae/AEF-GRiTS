from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest
import psutil

import webapp.app as web


def _base_params(**updates):
    params = {
        "workflow": "grid",
        "grid_scheme": "tessera_0p1",
        "project": "gee-test",
        "years": [2025],
        "grid_ids": ["grid_117.05_31.05"],
        "grid_selections": [
            {"grid_id": "grid_117.05_31.05", "lon": 117.05, "lat": 31.05}
        ],
        "block_size": 256,
        "workers": 2,
        "max_retries": 6,
        "checkpoint_every": 8,
    }
    params.update(updates)
    return params


def test_grid_commands_preserve_selected_scheme(tmp_path):
    mgrs = _base_params(
        grid_scheme="mgrs",
        grid_ids=["50RPU", "50RPV"],
        grid_selections=[{"grid_id": "50RPU"}, {"grid_id": "50RPV"}],
    )
    command = web._build_cmd(mgrs, tmp_path)
    assert command[command.index("--grid-scheme") + 1] == "mgrs"
    assert command[command.index("--tiles") + 1 :] == ["50RPU", "50RPV"]
    assert "--bbox" not in command
    assert "--tessera-tile" not in command

    tessera = web._build_cmd(_base_params(), tmp_path)
    assert tessera[tessera.index("--grid-scheme") + 1] == "tessera_0p1"
    assert tessera[tessera.index("--tessera-tile") + 1 :] == ["117.05", "31.05"]
    assert "--tiles" not in tessera


def test_point_command_requires_and_passes_samples(tmp_path):
    params = {
        "workflow": "points",
        "project": "gee-test",
        "years": [2025],
        "samples_path": str(tmp_path / "samples.csv"),
        "workers": 1,
        "max_retries": 6,
        "geometry_mode": "representative",
        "max_points": 1000,
        "id_field": "plot_id",
        "layer": "",
        "reference_grid": "",
    }
    command = web._build_cmd(params, tmp_path / "out")
    assert command[command.index("--samples") + 1] == params["samples_path"]
    assert command[command.index("--geometry-mode") + 1] == "representative"
    assert command[command.index("--id-field") + 1] == "plot_id"


def test_point_params_do_not_claim_a_grid_scheme():
    params = web._normalize_params(
        {"workflow": "point", "project": "gee-test", "years": [2025]}
    )
    assert params["grid_scheme"] is None


@pytest.mark.parametrize("years", [[], [2016], [2026], [2017, 2025, 2026]])
def test_year_validation_rejects_unsupported_values(years):
    with pytest.raises(ValueError):
        web._normalize_years(years)


def test_bbox_and_output_safety(tmp_path, monkeypatch):
    assert web._normalize_bbox([117, 31, 118, 32]) == [117.0, 31.0, 118.0, 32.0]
    with pytest.raises(ValueError):
        web._normalize_bbox([118, 31, 117, 32])
    monkeypatch.setattr(web, "OUTPUT_ROOT", tmp_path.resolve())
    assert web._safe_output_dir("anhui/2025", "run").is_relative_to(tmp_path)
    with pytest.raises(ValueError):
        web._safe_output_dir("../outside", "run")
    with pytest.raises(ValueError):
        web._safe_output_dir(str((tmp_path.parent / "absolute").resolve()), "run")


def test_output_directory_browser_is_confined_and_can_create(isolated_runner):
    root = isolated_runner.get("/api/output-directories")
    assert root.status_code == 200
    assert root.get_json()["path"] == ""

    created = isolated_runner.post(
        "/api/output-directories", json={"parent": "", "name": "research_outputs"}
    )
    assert created.status_code == 201
    assert created.get_json()["path"] == "research_outputs"

    listing = isolated_runner.get("/api/output-directories")
    assert "research_outputs" in [item["name"] for item in listing.get_json()["directories"]]
    nested = isolated_runner.get("/api/output-directories?path=research_outputs")
    assert nested.status_code == 200
    assert nested.get_json()["parent"] == ""

    assert isolated_runner.get("/api/output-directories?path=../escape").status_code == 400
    assert isolated_runner.post(
        "/api/output-directories", json={"parent": "", "name": "../escape"}
    ).status_code == 400


def test_tessera_plan_uses_positive_aoi_intersections():
    params = web._normalize_params(
        {
            "workflow": "grid",
            "gridScheme": "tessera_0p1",
            "project": "gee-test",
            "years": [2025],
            "bbox": {"west": 117.01, "south": 31.01, "east": 117.09, "north": 31.09},
        }
    )
    plan = web._plan_grid(params)
    assert plan["grid_count"] == 1
    assert plan["grid_ids"] == ["grid_117.05_31.05"]
    assert plan["raw_bytes"] > 0
    assert plan["aoi_crs"] == "EPSG:4326"
    assert plan["grid_crs"] == ["EPSG:32650"]
    assert plan["grid_features"]["features"][0]["properties"]["grid_id"] == "grid_117.05_31.05"


def test_multigrid_progress_is_aggregated(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path)
    rid = "progress-test"
    web._tasks[rid] = {
        "params": {"grid_ids": ["tile_a", "tile_b"]},
        "progress": 0.0,
        "grid_progress": {},
        "started_at": "",
        "status": "running",
        "output_dir": Path("output"),
        "log_path": Path("run.log"),
    }
    try:
        web._update_progress(rid, '  "grid_id": "tile_a",')
        web._update_progress(rid, "[10/10] 2025:0:0")
        assert web._tasks[rid]["progress"] == pytest.approx(0.5)
        web._update_progress(rid, '  "grid_id": "tile_b",')
        web._update_progress(rid, "[5/10] 2025:0:0")
        assert web._tasks[rid]["progress"] == pytest.approx(0.75)
    finally:
        web._tasks.pop(rid, None)


class _DormantExecutor:
    def submit(self, *args, **kwargs):
        return None


@pytest.fixture
def isolated_runner(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(web, "PLANS_DIR", tmp_path / "runs" / "plans")
    monkeypatch.setattr(web, "OUTPUT_ROOT", tmp_path / "output")
    web.RUNS_DIR.mkdir()
    web.PLANS_DIR.mkdir()
    web.OUTPUT_ROOT.mkdir()
    monkeypatch.setattr(web, "_executor", _DormantExecutor())
    monkeypatch.setattr(web, "_submission_slots", threading.BoundedSemaphore(22))
    web._tasks.clear()
    web._processes.clear()
    yield web.app.test_client()
    web._tasks.clear()
    web._processes.clear()


def test_grid_task_is_planned_persisted_and_cancellable(isolated_runner):
    plan_response = isolated_runner.post(
        "/api/plan",
        json={
            "workflow": "grid",
            "gridScheme": "tessera_0p1",
            "project": "gee-test",
            "years": [2025],
            "bbox": {"west": 117.01, "south": 31.01, "east": 117.09, "north": 31.09},
        },
    )
    assert plan_response.status_code == 200
    response = isolated_runner.post(
        "/api/tasks", json={"planId": plan_response.get_json()["plan_id"]}
    )
    assert response.status_code == 202
    rid = response.get_json()["run_id"]
    assert web._task_manifest(rid).exists()
    assert web._tasks[rid]["params"]["grid_ids"] == ["grid_117.05_31.05"]
    cancelled = isolated_runner.delete(f"/api/tasks/{rid}")
    assert cancelled.get_json()["status"] == "cancelled"


def test_cross_origin_task_submission_is_rejected(isolated_runner):
    response = isolated_runner.post(
        "/api/tasks",
        json={},
        headers={"Origin": "https://malicious.example"},
    )
    assert response.status_code == 403


def test_plan_is_signed_one_time_and_queue_is_bounded(isolated_runner, monkeypatch):
    payload = {
        "workflow": "grid",
        "gridScheme": "tessera_0p1",
        "project": "gee-test",
        "years": [2025],
        "bbox": {"west": 117.01, "south": 31.01, "east": 117.09, "north": 31.09},
    }
    planned = isolated_runner.post("/api/plan", json=payload).get_json()
    token = planned["plan_id"]
    first = isolated_runner.post("/api/tasks", json={"planId": token})
    assert first.status_code == 202
    second = isolated_runner.post("/api/tasks", json={"planId": token})
    assert second.status_code == 400
    assert "not reusable" in second.get_json()["error"]

    another = isolated_runner.post("/api/plan", json=payload).get_json()
    monkeypatch.setattr(web, "_submission_slots", threading.BoundedSemaphore(0))
    full = isolated_runner.post("/api/tasks", json={"planId": another["plan_id"]})
    assert full.status_code == 429


def test_large_plan_requires_exact_confirmation(isolated_runner, monkeypatch):
    monkeypatch.setattr(web, "WARN_RAW_BYTES", 1)
    payload = {
        "workflow": "grid",
        "gridScheme": "tessera_0p1",
        "project": "gee-test",
        "years": [2025],
        "bbox": {"west": 117.01, "south": 31.01, "east": 117.09, "north": 31.09},
    }
    plan = isolated_runner.post("/api/plan", json=payload).get_json()
    assert plan["strong_confirmation"]
    rejected = isolated_runner.post("/api/tasks", json={"planId": plan["plan_id"]})
    assert rejected.status_code == 400
    accepted = isolated_runner.post(
        "/api/tasks",
        json={"planId": plan["plan_id"], "confirmation": plan["confirmation_phrase"]},
    )
    assert accepted.status_code == 202


def test_disk_preflight_can_block_start(isolated_runner, monkeypatch):
    monkeypatch.setattr(web.shutil, "disk_usage", lambda _: SimpleNamespace(total=1, used=1, free=0))
    payload = {
        "workflow": "grid",
        "gridScheme": "tessera_0p1",
        "project": "gee-test",
        "years": [2025],
        "bbox": {"west": 117.01, "south": 31.01, "east": 117.09, "north": 31.09},
    }
    plan = isolated_runner.post("/api/plan", json=payload).get_json()
    assert not plan["can_start"]
    response = isolated_runner.post("/api/tasks", json={"planId": plan["plan_id"]})
    assert response.status_code == 400
    assert "Free disk" in response.get_json()["error"]


def test_structured_progress_is_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path)
    rid = "structured-progress"
    web._tasks[rid] = {
        "params": {"grid_ids": ["tile_a"]},
        "progress": 0.0,
        "grid_progress": {},
        "started_at": "",
        "status": "running",
        "output_dir": tmp_path / "output",
        "log_path": tmp_path / "run.log",
    }
    try:
        web._update_progress(
            rid,
            'AEF_EVENT {"event":"progress","workflow":"grid","grid_id":"tile_a","completed":3,"total":4,"eta_seconds":2}',
        )
        assert web._tasks[rid]["progress"] == pytest.approx(0.75)
        assert web._tasks[rid]["progress_detail"]["eta_seconds"] == 2
        assert (tmp_path / "events.jsonl").exists()
    finally:
        web._tasks.pop(rid, None)


def test_running_subprocess_is_really_terminated(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(web, "OUTPUT_ROOT", tmp_path / "output")
    web.RUNS_DIR.mkdir()
    web.OUTPUT_ROOT.mkdir()
    web._tasks.clear()


def test_restart_recovery_stops_exact_orphan_process(tmp_path, monkeypatch):
    run_root = tmp_path / "runs"
    run_dir = run_root / "orphan-run"
    run_dir.mkdir(parents=True)
    monkeypatch.setattr(web, "RUNS_DIR", run_root)
    process = __import__("subprocess").Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    create_time = psutil.Process(process.pid).create_time()
    manifest = {
        "run_id": "orphan-run",
        "status": "running",
        "progress": 0.2,
        "started_at": web._now(),
        "params": {"workflow": "points"},
        "output_dir": str(tmp_path / "output"),
        "pid": process.pid,
        "process_create_time": create_time,
    }
    (run_dir / "task.json").write_text(json.dumps(manifest), encoding="utf-8")
    web._tasks.clear()
    try:
        web._load_tasks()
        process.wait(timeout=10)
        assert web._tasks["orphan-run"]["status"] == "interrupted"
        assert "orphan process tree was stopped" in web._tasks["orphan-run"]["error"]
    finally:
        if process.poll() is None:
            process.kill()
        web._tasks.clear()
    web._processes.clear()
    monkeypatch.setattr(web, "_submission_slots", threading.BoundedSemaphore(1))
    assert web._submission_slots.acquire(blocking=False)
    rid = "running-cancel-test"
    log_path = web.RUNS_DIR / rid / "run.log"
    log_path.parent.mkdir()
    web._tasks[rid] = {
        "status": "queued",
        "progress": 0.0,
        "started_at": web._now(),
        "finished_at": "",
        "params": {"workflow": "points"},
        "log_path": log_path,
        "output_dir": web.OUTPUT_ROOT / rid,
        "cancel_event": threading.Event(),
        "grid_progress": {},
    }
    worker = threading.Thread(
        target=web._run_subprocess,
        args=(rid, [sys.executable, "-c", "import time; time.sleep(30)"], log_path),
        daemon=True,
    )
    worker.start()
    deadline = time.time() + 5
    while rid not in web._processes and time.time() < deadline:
        time.sleep(0.05)
    assert rid in web._processes
    response = web.app.test_client().delete(f"/api/tasks/{rid}")
    assert response.status_code == 200
    worker.join(10)
    assert not worker.is_alive()
    assert web._tasks[rid]["status"] == "cancelled"
    assert rid not in web._processes
    web._tasks.clear()


def test_point_upload_is_validated_before_queueing(isolated_runner):
    payload = {
        "workflow": "point",
        "project": "gee-test",
        "years": [2025],
        "pointIdField": "sample_id",
    }
    plan_response = isolated_runner.post(
        "/api/plan/points",
        data={
            "payload": __import__("json").dumps(payload),
            "samples": (io.BytesIO(b"sample_id,lon,lat\np1,117.05,31.05\n"), "points.csv"),
        },
        content_type="multipart/form-data",
    )
    assert plan_response.status_code == 200, plan_response.get_json()
    response = isolated_runner.post(
        "/api/tasks", json={"planId": plan_response.get_json()["plan_id"]}
    )
    assert response.status_code == 202, response.get_json()
    rid = response.get_json()["run_id"]
    params = web._tasks[rid]["params"]
    assert params["sample_count"] == 1
    assert Path(params["samples_path"]).exists()


def test_wkt_aoi_is_inspected_as_wgs84_polygon(isolated_runner):
    response = isolated_runner.post(
        "/api/aoi/inspect",
        data={
            "aoi": (
                io.BytesIO(
                    b"POLYGON ((117.01 31.01, 117.09 31.01, 117.09 31.09, 117.01 31.09, 117.01 31.01))"
                ),
                "anhui.wkt",
            )
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200, response.get_json()
    report = response.get_json()
    assert report["geometry_type"] == "Polygon"
    assert report["normalized_crs"] == "EPSG:4326"
    assert report["bbox"] == pytest.approx([117.01, 31.01, 117.09, 31.09])


def test_point_shapefile_rejects_polygon_geometry(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    source = tmp_path / "polygon.shp"
    gpd.GeoDataFrame({"sample_id": ["p1"]}, geometry=[box(117, 31, 118, 32)], crs="EPSG:4326").to_file(source)
    with pytest.raises(ValueError, match="only Point/MultiPoint"):
        web._inspect_point_geometry(source)


def test_point_plan_does_not_start_until_confirmed(isolated_runner):
    payload = {"workflow": "point", "project": "gee-test", "years": [2025]}
    response = isolated_runner.post(
        "/api/plan/points",
        data={
            "payload": json.dumps(payload),
            "samples": (io.BytesIO(b"sample_id,lon,lat\np1,117.05,31.05\n"), "points.csv"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    plan = response.get_json()
    assert plan["rows"] == 1
    assert plan["normalized_crs"] == "EPSG:4326"
    assert web._tasks == {}


def test_point_output_validation_and_results_endpoint(tmp_path):
    output = tmp_path / "output"
    shards = output / "shards"
    shards.mkdir(parents=True)
    shard = shards / "part00001.parquet"
    import pandas as pd

    pd.DataFrame({"sample_id": ["p1"]}).to_parquet(shard, index=False)
    pd.DataFrame({"path": [str(shard)]}).to_parquet(output / "catalog.parquet", index=False)
    (output / "report.json").write_text(
        json.dumps({"sample_count": 1, "complete_rows": 1, "incomplete_rows": 0}),
        encoding="utf-8",
    )
    task = {
        "params": {"workflow": "points", "sample_count": 1, "years": [2025]},
        "output_dir": output,
    }
    validation = web._validate_task_outputs(task)
    assert validation["valid"]
    web._tasks["result-test"] = {
        **task,
        "status": "done",
        "progress": 1.0,
        "started_at": "",
        "validation": validation,
        "provenance": {"git_commit": "abc"},
    }
    try:
        response = web.app.test_client().get("/api/tasks/result-test/results")
        assert response.status_code == 200
        assert response.get_json()["validation"]["valid"]
    finally:
        web._tasks.pop("result-test", None)

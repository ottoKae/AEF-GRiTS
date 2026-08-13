from __future__ import annotations

from aef_grits.doctor import run_checks


def test_offline_download_environment_doctor(tmp_path):
    report = run_checks(
        mode="all",
        project=None,
        output=tmp_path / "downloads",
        minimum_free_gib=0,
        skip_ee=True,
    )
    assert report["ok"], report
    statuses = {item["name"]: item["status"] for item in report["checks"]}
    assert statuses["output"] == "pass"
    assert statuses["proj"] == "pass"
    assert statuses["zarr_v3"] == "pass"
    assert statuses["mgrs_index"] == "pass"
    assert statuses["package:flask"] == "pass"
    assert statuses["package:psutil"] == "pass"
    assert statuses["earth_engine"] == "skip"

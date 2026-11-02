"""Offline checks for explicit AOI selection and honest coverage reporting."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_aef_coverage.py"
SPEC = importlib.util.spec_from_file_location("check_aef_coverage_under_test", SCRIPT)
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)

POLYGON = {
    "type": "Polygon",
    "coordinates": [[[114.0, 30.0], [115.0, 30.0], [115.0, 31.0],
                     [114.0, 31.0], [114.0, 30.0]]],
}


def options(*selection):
    return ["--years", "2023", "2024", "2025", "--out-dir", "unused", *selection]


@pytest.mark.parametrize(
    ("selection", "level", "filters"),
    [
        (["--country", "China"], 0, {"ADM0_NAME": "China"}),
        (["--country", "China", "--admin1", "Hubei Sheng"], 1,
         {"ADM0_NAME": "China", "ADM1_NAME": "Hubei Sheng"}),
        (["--country", "China", "--admin1", "Hubei Sheng", "--admin2", "Wuhan Shi"], 2,
         {"ADM0_NAME": "China", "ADM1_NAME": "Hubei Sheng", "ADM2_NAME": "Wuhan Shi"}),
    ],
)
def test_administrative_selection_uses_explicit_hierarchy(selection, level, filters):
    args = coverage.parse_args(options(*selection))
    assert coverage.admin_selection(args) == (f"FAO/GAUL/2015/level{level}", filters)


def test_non_wuhan_bbox_and_grid_are_configurable():
    args = coverage.parse_args(options(
        "--bbox", "-75", "-2", "-74", "-1", "--scale", "30", "--crs", "EPSG:32718",
    ))
    assert args.bbox == [-75, -2, -74, -1]
    assert args.scale == 30
    assert args.crs == "EPSG:32718"
    assert args.years == [2023, 2024, 2025]


def test_default_grid_is_not_a_wuhan_utm_zone():
    args = coverage.parse_args(options("--country", "Ecuador"))
    assert args.scale == 10
    assert args.crs == "EPSG:6933"


@pytest.mark.parametrize("argv", [
    ["--country", "China", "--out-dir", "unused"],
    ["--country", "China", "--years", "2024"],
    options(),
    options("--country", "China", "--bbox", "114", "30", "115", "31"),
    options("--aoi", "area.geojson", "--country", "China"),
    options("--country", "China", "--admin2", "Wuhan Shi"),
    options("--aoi", "area.geojson", "--admin1", "Hubei Sheng"),
    options("--bbox", "114", "30", "115", "31", "--admin2", "Wuhan Shi"),
])
def test_ambiguous_or_incomplete_selection_fails_before_query(argv):
    with pytest.raises(SystemExit) as error:
        coverage.parse_args(argv)
    assert error.value.code == 2


@pytest.mark.parametrize("bounds", [
    ["115", "30", "114", "31"],
    ["114", "31", "115", "30"],
    ["114", "30", "114", "31"],
    ["114", "30", "115", "30"],
    ["170", "-10", "-170", "10"],
    ["-181", "30", "115", "31"],
    ["114", "30", "181", "31"],
    ["114", "-91", "115", "31"],
    ["114", "30", "115", "91"],
    ["nan", "30", "115", "31"],
    ["114", "30", "inf", "31"],
])
def test_invalid_bbox_does_not_silently_select_another_area(bounds):
    with pytest.raises(SystemExit) as error:
        coverage.parse_args(options("--bbox", *bounds))
    assert error.value.code == 2


@pytest.mark.parametrize("scale", ["0", "-1", "nan", "inf"])
def test_statistics_scale_must_be_positive_and_finite(scale):
    with pytest.raises(SystemExit) as error:
        coverage.parse_args(options("--country", "China", "--scale", scale))
    assert error.value.code == 2


@pytest.mark.parametrize("representation", ["geometry", "feature", "collection"])
def test_geojson_accepts_common_polygon_containers_without_changing_the_area(tmp_path, representation):
    feature = {"type": "Feature", "properties": {"name": "study area"}, "geometry": POLYGON}
    payload = {"geometry": POLYGON, "feature": feature,
               "collection": {"type": "FeatureCollection", "features": [feature]}}[representation]
    path = tmp_path / "area.geojson"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = coverage.load_geojson(path)
    assert result["type"] == "FeatureCollection"
    assert len(result["features"]) == 1
    assert result["features"][0]["geometry"] == POLYGON
    if representation != "geometry":
        assert result["features"][0]["properties"] == feature["properties"]


def test_geojson_preserves_multipart_area_and_holes(tmp_path):
    polygon = copy.deepcopy(POLYGON)
    polygon["coordinates"].append([
        [114.2, 30.2], [114.2, 30.4], [114.4, 30.4], [114.4, 30.2], [114.2, 30.2],
    ])
    payload = {"type": "MultiPolygon", "coordinates": [polygon["coordinates"]]}
    path = tmp_path / "multipart.geojson"
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = coverage.load_geojson(path)
    assert result["features"][0]["geometry"] == payload


@pytest.mark.parametrize("payload", [
    {"type": "FeatureCollection", "features": []},
    {"type": "Feature", "properties": {}, "geometry": None},
    {"type": "Point", "coordinates": [114, 30]},
    {"type": "Polygon", "coordinates": []},
    {"type": "Polygon", "coordinates": [[[500000, 3300000], [500100, 3300000],
                                           [500100, 3300100], [500000, 3300000]]]},
    {"type": "Polygon", "coordinates": [[[114, 91], [115, 91], [115, 92], [114, 91]]]},
    {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
     "features": [{"type": "Feature", "properties": {}, "geometry": POLYGON}]},
    {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": POLYGON},
        {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [114, 30]}},
    ]},
])
def test_empty_nonpolygon_or_projected_geojson_is_rejected(tmp_path, payload):
    path = tmp_path / "invalid.geojson"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        coverage.load_geojson(path)


@pytest.mark.parametrize(("valid", "missing", "expected"), [
    (100, 0, (100, 0)), (0, 100, (0, 100)), (75, 25, (75, 25)),
])
def test_coverage_denominator_includes_uncovered_area(valid, missing, expected):
    assert coverage.coverage_percentages(
        {"total_m2": 100, "valid_m2": valid, "missing_m2": missing},
    ) == expected


@pytest.mark.parametrize("stats", [
    {},
    {"total_m2": 100, "valid_m2": 100, "missing_m2": None},
    {"total_m2": 0, "valid_m2": 0, "missing_m2": 0},
    {"total_m2": 100, "valid_m2": float("nan"), "missing_m2": 0},
    {"total_m2": float("inf"), "valid_m2": 100, "missing_m2": 0},
    {"total_m2": 100, "valid_m2": -1, "missing_m2": 101},
    {"total_m2": 100, "valid_m2": 90, "missing_m2": 0},
    {"total_m2": 100, "valid_m2": 100.1, "missing_m2": 0},
])
def test_invalid_statistics_never_become_a_complete_coverage_claim(stats):
    with pytest.raises(ValueError):
        coverage.coverage_percentages(stats)

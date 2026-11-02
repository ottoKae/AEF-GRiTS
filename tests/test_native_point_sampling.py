from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from affine import Affine
import numpy as np
import pandas as pd
from pyproj import Transformer
import pytest

from aef_grits.earth_engine import feature_columns
from aef_grits.native_grid import NATIVE_PROTOCOL, NativeSource
from aef_grits.point_source import native_point_locations, point_run_signature
from scripts import stream_aef_points_ee as streamer


@pytest.mark.parametrize("pixel_y", [10, -10])
def test_native_locations_use_containing_pixel_and_signed_grid(pixel_y):
    source = NativeSource("asset", 2025, "EPSG:32649", Affine(10, 0, 790000, 0, pixel_y, 3400000),
                          32, 32, 1, {})
    x, y = source.transform * (7.2, 12.8)
    lon, lat = Transformer.from_crs(source.crs, "EPSG:4326", always_xy=True).transform(x, y)
    frame = pd.DataFrame({"sample_id": ["p"], "lon": [lon], "lat": [lat]})
    result = native_point_locations(frame, source).iloc[0]
    assert (result.column, result.row) == (7, 12)
    assert (result.center_x, result.center_y) == source.transform * (7.5, 12.5)
    outside = frame.copy()
    outside["lon"] = -80
    assert native_point_locations(outside, source).empty


class FakeImage:
    def __init__(self, owner, asset):
        self.owner, self.asset = owner, asset

    def select(self, bands):
        assert len(bands) == 64
        return self

    def toFloat(self):
        return self

    def mask(self):
        return self

    def reduce(self, reducer):
        assert reducer == "min"
        return self

    def updateMask(self, mask):
        return self

    def rename(self, bands):
        self.bands = bands
        return self

    def sampleRegions(self, **kwargs):
        assert "projection" in kwargs
        assert "scale" not in kwargs
        self.owner.calls.append((self.asset, kwargs))
        return self, kwargs


class FakeEE:
    """Only replace the network boundary; routing and projection code remain real."""

    def __init__(self, values):
        self.values, self.calls = values, []
        self.Geometry = SimpleNamespace(
            MultiPoint=lambda coordinates, crs: ("MultiPoint", coordinates, crs),
            Point=lambda coordinates, crs: (coordinates, crs),
        )
        self.Reducer = SimpleNamespace(min=lambda: "min")
        self.data = SimpleNamespace(computeFeatures=self.compute_features)

    def Image(self, asset):
        return FakeImage(self, asset)

    @staticmethod
    def Feature(geometry, properties):
        return geometry, properties

    @staticmethod
    def FeatureCollection(features):
        return features

    @staticmethod
    def Projection(crs, transform):
        return crs, transform

    def compute_features(self, query):
        image, kwargs = query["expression"]
        records = []
        for _, properties in kwargs["collection"]:
            sample_id = properties["sample_id"]
            vector = self.values.get((image.asset, sample_id))
            if vector is not None:
                records.append({"sample_id": sample_id, **dict(zip(image.bands, vector))})
        return pd.DataFrame(records)


@pytest.fixture
def scene(monkeypatch):
    sources = []
    for asset, crs in (("a", "EPSG:32650"), ("b", "EPSG:32649"), ("c", "EPSG:32649")):
        x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(113.999, 30)
        transform = Affine(10, 0, np.floor(x / 10) * 10 - 100, 0, 10, np.floor(y / 10) * 10 - 100)
        sources.append(NativeSource(asset, 2025, crs, transform, 32, 32, 1, {}))

    def source_iterator(ee, year, geometry, request):
        assert geometry[0] == "MultiPoint"
        return iter(sources if year == 2025 else [])

    monkeypatch.setattr(streamer, "iter_native_sources", source_iterator)
    vector = np.arange(64, dtype=np.float32)
    values = {(asset, point): vector + shift for asset, shift in (("a", 100), ("b", 200), ("c", 300))
              for point in ("p1", "p2", "p3")}
    values["b", "p1"] = None
    values["c", "p1"] = None
    values["b", "p3"] = np.full(64, np.nan)
    ee = FakeEE(values)
    points = pd.DataFrame({"sample_id": ["p1", "p2", "p3"], "lon": [113.999] * 3, "lat": [30.] * 3})
    return ee, points, sources


def request(ee, frame, years=(2024, 2025)):
    return streamer._request_chunk(
        ee, frame, list(years), scale=10, tile_scale=8, page_size=1000, max_retries=0,
        request_pool=SimpleNamespace(token=nullcontext),
    )


def test_local_utm_priority_fallback_missing_year_and_chunk_invariance(scene):
    ee, points, sources = scene
    whole = request(ee, points)
    assert whole.aef2025_source_asset.tolist() == ["a", "b", "c"]
    assert whole.aef2025_source_crs.tolist() == ["EPSG:32650", "EPSG:32649", "EPSG:32649"]
    assert whole[feature_columns([2024])].isna().all().all()
    assert whole.aef2024_source_asset.isna().all()
    assert (whole[feature_columns([2025])].dtypes == np.float32).all()
    split = pd.concat([request(ee, points.iloc[[index]]) for index in range(3)], ignore_index=True)
    pd.testing.assert_frame_equal(whole, split)
    for asset, kwargs in ee.calls:
        source = next(item for item in sources if item.id == asset)
        assert kwargs["projection"] == (source.crs, list(source.transform)[:6])
        for (coordinates, crs), properties in kwargs["collection"]:
            point = points.loc[points.sample_id == properties["sample_id"]]
            expected = native_point_locations(point, source).iloc[0]
            assert coordinates == [expected.center_x, expected.center_y]
            assert crs == source.crs
    manifest = whole.attrs["native_manifest"]
    assert manifest["protocol"] == NATIVE_PROTOCOL
    assert all(source["version"] == 1 for source in manifest["sources"])


@pytest.mark.parametrize("change", ["version", "new_asset"])
def test_shard_provenance_resume_and_source_version_rejection(scene, tmp_path, change):
    ee, points, sources = scene
    path = tmp_path / "part00001.parquet"
    args = SimpleNamespace(overwrite=False, scale=10, tile_scale=8, page_size=1000,
                           max_retries=0, request_timeout_seconds=30)
    pool = SimpleNamespace(token=nullcontext)
    columns = feature_columns([2024, 2025])
    first = streamer._complete_chunk(ee, 1, points, [2024, 2025], columns, args, path, pool)
    assert first["status"] == "downloaded"
    stored = pd.read_parquet(path)
    assert not stored.aef2024_complete.any()
    assert stored.aef2025_complete.all()
    assert not stored.aef_complete_all_years.any()
    assert path.with_suffix(".native.json").exists()
    ee.calls.clear()
    second = streamer._complete_chunk(ee, 1, points, [2024, 2025], columns, args, path, pool)
    assert second["status"] == "reused"
    assert not ee.calls
    if change == "version":
        sources[0] = replace(sources[0], version=2)
    else:
        sources.insert(0, replace(sources[0], id="00-new-source"))
    with pytest.raises(ValueError, match="sources changed"):
        streamer._complete_chunk(ee, 1, points, [2024, 2025], columns, args, path, pool)


@pytest.mark.parametrize("overwrite", [False, True])
def test_legacy_shard_cannot_be_silently_adopted(tmp_path, overwrite):
    path = tmp_path / "legacy.parquet"
    pd.DataFrame({"sample_id": ["p"]}).to_parquet(path)
    with pytest.raises(ValueError, match="no native provenance"):
        streamer._complete_chunk(None, 1, pd.DataFrame(), [2025], [],
                                 SimpleNamespace(overwrite=overwrite), path, None)


def test_point_signature_binds_native_protocol():
    parameters = dict(scale=10, tile_scale=8, chunk_size=1000, dataset="aef")
    actual = point_run_signature("hash", [2025], **parameters)
    payload = {**parameters, "scale": 10., "sample_sha256": "hash", "years": [2025]}
    legacy = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    assert actual != legacy
    payload["sampling_protocol"] = NATIVE_PROTOCOL
    assert actual == hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def test_cli_refuses_legacy_protocol_even_with_overwrite(tmp_path):
    samples = tmp_path / "points.csv"
    pd.DataFrame({"sample_id": ["p"], "lon": [114.], "lat": [30.]}).to_csv(samples, index=False)
    output = tmp_path / "legacy"
    state = output / ".aef_state"
    state.mkdir(parents=True)
    (state / "run.json").write_text(json.dumps({"signature": "legacy"}), encoding="utf-8")
    validation_path = state / "validation_report.json"
    original_validation = '{"sampling_protocol": "legacy", "valid": true}\n'
    validation_path.write_text(original_validation, encoding="utf-8")
    process = subprocess.run([
        sys.executable, "-B", "-m", "scripts.stream_aef_points_ee", "--samples", str(samples),
        "--out-dir", str(output), "--years", "2025", "--overwrite",
        "--resource-state-dir", str(tmp_path / "resources"),
    ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert process.returncode != 0
    assert "Legacy point sampling cannot be mixed" in process.stderr
    assert json.loads((state / "run.json").read_text())["signature"] == "legacy"
    assert validation_path.read_text(encoding="utf-8") == original_validation
    assert not (state / "point_preflight.sqlite").exists()
    assert not (output / "validation_report.json").exists()

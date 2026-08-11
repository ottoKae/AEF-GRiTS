"""Build a fixed, spatially balanced mainland-Ecuador AEF background pool."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys

import geopandas as gpd
from mgrs import MGRS
import numpy as np
import pandas as pd
from pyproj import Transformer
import shapely
from shapely.strtree import STRtree
from sklearn.neighbors import NearestNeighbors


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aef_grits.catalog import spatial_block_id  # noqa: E402


DEFAULT_OUT_DIR = ROOT / "outputs/global_background_2025"
LCLU_LAYER = "SC_COBERTURA_TIERRA_A"
TARGET_CRS = "EPSG:32717"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lclu", type=Path, required=True)
    parser.add_argument("--lclu-layer", default=LCLU_LAYER)
    parser.add_argument("--plantations", type=Path, required=True)
    parser.add_argument("--verified-negatives", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--target-points", type=int, default=40_000)
    parser.add_argument("--seed", type=int, default=20260802)
    parser.add_argument("--block-size-m", type=float, default=6000.0)
    parser.add_argument("--max-points-per-block", type=int, default=10)
    parser.add_argument("--known-sample-buffer-m", type=float, default=30.0)
    parser.add_argument("--candidate-batch", type=int, default=100_000)
    return parser.parse_args()


def _plantation_tree(path: Path, buffer_m: float) -> tuple[STRtree, int]:
    polygons = gpd.read_file(path, columns=[]).to_crs(TARGET_CRS)
    geometry = shapely.make_valid(polygons.geometry.to_numpy())
    usable = (~shapely.is_empty(geometry)) & (~shapely.is_missing(geometry))
    geometry = geometry[usable]
    if buffer_m > 0:
        geometry = shapely.buffer(geometry, float(buffer_m))
    return STRtree(geometry), int(len(geometry))


def _verified_negative_neighbours(path: Path) -> tuple[NearestNeighbors | None, int]:
    if not path.exists():
        return None, 0
    frame = pd.read_parquet(path)
    local = frame.loc[
        frame.label.eq(0) & frame.negative_group.isin(["crop", "stable_easy"]),
        ["model_center_x", "model_center_y"],
    ].dropna()
    if local.empty:
        return None, 0
    model = NearestNeighbors(n_neighbors=1).fit(local.to_numpy(dtype=np.float64))
    return model, int(len(local))


def _random_inside_candidates(
    geometry: np.ndarray,
    source_indices: np.ndarray,
    probabilities: np.ndarray,
    count: int,
    rng: np.random.Generator,
) -> tuple[np.ndarray, np.ndarray]:
    selected = rng.choice(source_indices, size=int(count), replace=True, p=probabilities)
    chosen = geometry[selected]
    bounds = shapely.bounds(chosen)
    x = rng.uniform(bounds[:, 0], bounds[:, 2])
    y = rng.uniform(bounds[:, 1], bounds[:, 3])
    points = shapely.points(x, y)
    inside = shapely.covers(chosen, points)
    return selected[inside], points[inside]


def build_background(
    lclu_path: Path,
    plantation_path: Path,
    negative_path: Path,
    *,
    layer: str = LCLU_LAYER,
    target_points: int = 40_000,
    seed: int = 20260802,
    block_size_m: float = 6000.0,
    max_points_per_block: int = 10,
    known_sample_buffer_m: float = 30.0,
    candidate_batch: int = 100_000,
) -> tuple[pd.DataFrame, dict]:
    if target_points <= 0 or block_size_m <= 0 or max_points_per_block <= 0:
        raise ValueError("target, block size, and block cap must be positive")
    if known_sample_buffer_m < 0 or candidate_batch <= 0:
        raise ValueError("buffer must be non-negative and candidate batch positive")
    if not lclu_path.exists() or not plantation_path.exists():
        raise FileNotFoundError("LCLU geodatabase or plantation shapefile is absent")

    lclu = gpd.read_file(
        lclu_path, layer=layer, columns=["niv1", "niv2", "niv3"],
        engine="pyogrio", use_arrow=True,
    )
    if lclu.crs is None:
        raise ValueError("LCLU layer has no CRS")
    lclu = lclu.to_crs(TARGET_CRS).reset_index(names="lclu_source_index")
    geometry = lclu.geometry.to_numpy()
    area = shapely.area(geometry)
    valid = (
        shapely.is_valid(geometry) & ~shapely.is_empty(geometry)
        & ~shapely.is_missing(geometry) & np.isfinite(area) & (area > 0)
    )
    source_indices = np.flatnonzero(valid)
    probabilities = area[source_indices] / area[source_indices].sum()
    plantation_tree, plantation_count = _plantation_tree(
        plantation_path, known_sample_buffer_m,
    )
    negative_neighbours, negative_count = _verified_negative_neighbours(negative_path)

    rng = np.random.default_rng(seed)
    block_counts: Counter[str] = Counter()
    accepted_points: list = []
    accepted_sources: list[int] = []
    diagnostics: Counter[str] = Counter()
    batches = 0
    while len(accepted_points) < target_points:
        batches += 1
        if batches > 100:
            raise RuntimeError(
                f"failed to reach {target_points} points after {batches - 1} batches; "
                f"retained={len(accepted_points)}"
            )
        needed = target_points - len(accepted_points)
        draw = max(int(candidate_batch), needed * 4)
        polygon_indices, points = _random_inside_candidates(
            geometry, source_indices, probabilities, draw, rng,
        )
        diagnostics["candidate_bbox_draws"] += draw
        diagnostics["candidate_inside_lclu"] += len(points)
        if not len(points):
            continue

        plantation_hits = plantation_tree.query(points, predicate="within")
        blocked = np.zeros(len(points), dtype=bool)
        if plantation_hits.size:
            blocked[np.unique(plantation_hits[0])] = True
        diagnostics["rejected_known_plantation"] += int(blocked.sum())

        point_xy = shapely.get_coordinates(points)
        if negative_neighbours is not None:
            distance = negative_neighbours.kneighbors(
                point_xy, return_distance=True,
            )[0][:, 0]
            near_negative = distance < float(known_sample_buffer_m)
            diagnostics["rejected_near_verified_negative"] += int((near_negative & ~blocked).sum())
            blocked |= near_negative

        for source_index, point, xy, is_blocked in zip(
            polygon_indices, points, point_xy, blocked,
        ):
            if is_blocked:
                continue
            block = spatial_block_id(float(xy[0]), float(xy[1]), block_size_m)
            if block_counts[block] >= max_points_per_block:
                diagnostics["rejected_block_cap"] += 1
                continue
            block_counts[block] += 1
            accepted_sources.append(int(source_index))
            accepted_points.append(point)
            if len(accepted_points) >= target_points:
                break

    coordinates = shapely.get_coordinates(np.asarray(accepted_points, dtype=object))
    sources = lclu.iloc[accepted_sources].reset_index(drop=True)
    to_wgs84 = Transformer.from_crs(TARGET_CRS, "EPSG:4326", always_xy=True)
    longitude, latitude = to_wgs84.transform(coordinates[:, 0], coordinates[:, 1])
    mgrs_encoder = MGRS()
    tiles = [
        mgrs_encoder.toMGRS(float(lat), float(lon), MGRSPrecision=0)[:5]
        for lon, lat in zip(longitude, latitude)
    ]
    result = pd.DataFrame({
        "model_center_x": coordinates[:, 0],
        "model_center_y": coordinates[:, 1],
        "lon": longitude,
        "lat": latitude,
        "tile_id": tiles,
        "lclu_source_index": sources.lclu_source_index.to_numpy(),
        "lclu_niv1": sources.niv1.astype(str).to_numpy(),
        "lclu_niv2": sources.niv2.astype(str).to_numpy(),
        "lclu_niv3": sources.niv3.astype(str).to_numpy(),
    })
    result["spatial_block_id"] = [
        spatial_block_id(x, y, block_size_m)
        for x, y in zip(result.model_center_x, result.model_center_y)
    ]
    result = result.sort_values(
        ["spatial_block_id", "model_center_x", "model_center_y"],
        kind="stable",
    ).reset_index(drop=True)
    result.insert(0, "sample_id", [f"background_2025_{i:06d}" for i in range(1, len(result) + 1)])
    result["sample_type"] = "unlabeled_global_background"
    result["label_status"] = "unlabeled_not_verified_negative"
    result["sample_seed"] = int(seed)
    result["block_size_m"] = float(block_size_m)
    result["max_points_per_block"] = int(max_points_per_block)
    result["source_crs"] = TARGET_CRS

    if result.sample_id.duplicated().any():
        raise AssertionError("background sample IDs are not unique")
    if result.groupby("spatial_block_id").size().max() > max_points_per_block:
        raise AssertionError("spatial block cap was violated")
    if not np.isfinite(result[["lon", "lat"]]).all().all():
        raise AssertionError("background coordinates are not finite")
    report = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_lclu": str(lclu_path.resolve()), "source_layer": layer,
        "source_lclu_features": int(len(lclu)),
        "source_lclu_valid_features": int(valid.sum()),
        "excluded_invalid_or_empty_lclu_features": int((~valid).sum()),
        "plantation_exclusion_features": plantation_count,
        "verified_negative_exclusion_points": negative_count,
        "target_points": int(target_points), "retained_points": int(len(result)),
        "seed": int(seed), "block_size_m": float(block_size_m),
        "max_points_per_block": int(max_points_per_block),
        "known_sample_buffer_m": float(known_sample_buffer_m),
        "occupied_spatial_blocks": int(result.spatial_block_id.nunique()),
        "maximum_retained_per_block": int(result.groupby("spatial_block_id").size().max()),
        "mgrs_tiles": int(result.tile_id.nunique()),
        "tile_counts": result.tile_id.value_counts().sort_index().to_dict(),
        "diagnostics": {key: int(value) for key, value in diagnostics.items()},
        "semantics": (
            "unlabeled background for manifold context and PU learning; "
            "never a verified negative class"
        ),
    }
    return result, report


def main() -> None:
    args = parse_args()
    samples, report = build_background(
        args.lclu, args.plantations, args.verified_negatives,
        layer=args.lclu_layer, target_points=args.target_points, seed=args.seed,
        block_size_m=args.block_size_m,
        max_points_per_block=args.max_points_per_block,
        known_sample_buffer_m=args.known_sample_buffer_m,
        candidate_batch=args.candidate_batch,
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "global_background_points_2025.csv"
    parquet_path = args.out_dir / "global_background_points_2025.parquet"
    report_path = args.out_dir / "global_background_sampling_report.json"
    samples.to_csv(csv_path, index=False)
    samples.to_parquet(parquet_path, index=False)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "outputs": {"csv": str(csv_path), "parquet": str(parquet_path),
                    "report": str(report_path)}, **report,
    }, indent=2), flush=True)


if __name__ == "__main__":
    main()

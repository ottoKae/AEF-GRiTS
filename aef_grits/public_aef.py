"""Experimental native-UTM AEF COG reads from a verified, local official index.

This module does not download the global index or start an Earth Engine login.
The GUI must not advertise a complete anonymous extraction workflow until its
inventory acquisition and worker integration have also been validated.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd
from pyproj import Transformer
import rasterio
from rasterio.warp import transform_bounds
from rasterio.windows import Window
import requests

from .grids import GridSpec
from .native_grid import NATIVE_PROTOCOL, NativeAlignmentError, pixel_footprints_match


PUBLIC_ROOT = "https://storage.googleapis.com/alphaearth_foundations/"
INDEX_CSV_URL = PUBLIC_ROOT + "satellite_embedding/v1/annual/aef_index.csv"
INDEX_PARQUET_URL = PUBLIC_ROOT + "satellite_embedding/v1/annual/aef_index.parquet"
BANDS = tuple(f"A{index:02d}" for index in range(64))
INDEX_COLUMNS = (
    "path", "crs", "year", "utm_west", "utm_south", "utm_east", "utm_north",
    "wgs84_west", "wgs84_south", "wgs84_east", "wgs84_north",
)
PROVENANCE = {
    "source": "official_gcs",
    "dataset": "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL",
    "encoding": "signed_int8",
    "nodata": -128,
    "decode": "sign(q) * (q / 127.5)^2",
    "resampling": "none",
    "native_protocol": NATIVE_PROTOCOL,
    "license": "CC-BY-4.0",
}


def probe_public_source() -> dict:
    """Check anonymous Range access with at most 264 response bytes.

    Reachability is not an extraction-ready claim: the global catalog is large
    and intentionally is not fetched by a connection check.
    """
    sizes = {}
    for name, url, requested_range, limit in (
        ("csv", INDEX_CSV_URL, "bytes=0-255", 256),
        ("parquet", INDEX_PARQUET_URL, "bytes=-8", 8),
    ):
        with requests.get(
            url, headers={"Range": requested_range}, stream=True, timeout=(10, 20)
        ) as response:
            response.raise_for_status()
            if response.status_code != 206:
                raise ValueError("Public source did not honor the bounded Range request")
            content_range = response.headers.get("Content-Range", "")
            try:
                sizes[name] = int(content_range.rsplit("/", 1)[1])
            except (IndexError, ValueError) as exc:
                raise ValueError("Public source returned an invalid Content-Range") from exc
            payload = response.raw.read(limit)
            if name == "csv" and not payload.startswith(b"WKT,crs,path,year,utm_zone,"):
                raise ValueError("Official AEF index schema was not recognized")
            if name == "parquet" and (len(payload) != 8 or payload[-4:] != b"PAR1"):
                raise ValueError("Official AEF Parquet footer was not recognized")
    return {
        "source": "official_gcs",
        "reachable": True,
        "range_supported": True,
        "index_url": INDEX_PARQUET_URL,
        "index_bytes": sizes["parquet"],
        "csv_index_bytes": sizes["csv"],
        "extraction_ready": False,
        "note": "Connection verified; full inventory acquisition and GUI extraction are not enabled.",
    }


class PublicAEFIndex:
    """Official index bounds only; query results do not prove valid pixels.

    A local Parquet is explicitly supplied by the caller. Geometry columns are
    not loaded, so matches are conservative bounding-box candidates.
    """

    def __init__(self, frame: pd.DataFrame):
        missing = set(INDEX_COLUMNS) - set(frame.columns)
        if missing:
            raise ValueError(f"Official AEF index is missing columns: {sorted(missing)}")
        self.frame = frame.loc[:, INDEX_COLUMNS].copy()
        if self.frame.isna().any().any():
            raise ValueError("Official AEF index contains missing required values")
        years = pd.to_numeric(self.frame["year"], errors="raise")
        if not np.isfinite(years).all() or not np.equal(years, np.floor(years)).all():
            raise ValueError("Official AEF index years must be finite integers")
        self.frame["year"] = years.astype(int)
        for column in INDEX_COLUMNS[3:]:
            self.frame[column] = pd.to_numeric(self.frame[column], errors="raise")
        if not np.isfinite(self.frame.loc[:, INDEX_COLUMNS[3:]].to_numpy()).all():
            raise ValueError("Official AEF index bounds must be finite")
        for prefix in ("utm", "wgs84"):
            if (
                (self.frame[f"{prefix}_west"] >= self.frame[f"{prefix}_east"])
                | (self.frame[f"{prefix}_south"] >= self.frame[f"{prefix}_north"])
            ).any():
                raise ValueError("Official AEF index bounds are reversed or empty")
        self.frame["path"] = self.frame["path"].map(_source_path)
        for crs in self.frame["crs"].unique():
            parsed = rasterio.crs.CRS.from_user_input(crs)
            epsg = parsed.to_epsg()
            if epsg is None or not (32601 <= epsg <= 32660 or 32701 <= epsg <= 32760):
                raise ValueError(f"Official AEF source must use a UTM CRS, got {crs}")
        self.frame = self.frame.sort_values(["year", "path"]).reset_index(drop=True)

    @classmethod
    def from_parquet(cls, path: str | Path) -> "PublicAEFIndex":
        return cls(pd.read_parquet(path, columns=list(INDEX_COLUMNS)))

    def query(self, bbox: Sequence[float], years: Sequence[int]) -> pd.DataFrame:
        west, south, east, north = map(float, bbox)
        if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
            raise ValueError("Query must be a WGS84 bbox; split antimeridian queries")
        frame = self.frame
        selected = (
            frame["year"].isin(years)
            & (frame["wgs84_west"] <= east) & (frame["wgs84_east"] >= west)
            & (frame["wgs84_south"] <= north) & (frame["wgs84_north"] >= south)
        )
        return frame.loc[selected].copy()

    def read_grid_block(self, spec: GridSpec, year: int, window: Window) -> np.ndarray:
        """Read matching native pixels as float32 [64,H,W], NaN when masked.

        A different UTM zone or fractional-pixel shift is not resampled.
        """
        if (
            window.width <= 0 or window.height <= 0
            or any(value != int(value) for value in window.flatten())
            or window.col_off < 0 or window.row_off < 0
            or window.col_off + window.width > spec.width
            or window.row_off + window.height > spec.height
        ):
            raise ValueError("Window must contain whole pixels inside the target grid")
        # The public GUI schedules small blocks, never a whole tile allocation.
        if window.width > 512 or window.height > 512:
            raise ValueError("Read windows are limited to 512 x 512 pixels")
        target_bounds = rasterio.windows.bounds(window, spec.transform)
        bbox = transform_bounds(spec.crs, "EPSG:4326", *target_bounds, densify_pts=21)
        candidates = self.query(bbox, [year])
        output = np.full((64, int(window.height), int(window.width)), np.nan, np.float32)
        compatible_sources = 0
        for row in candidates.itertuples(index=False):
            with _open_source(row.path) as source:
                _check_raster(source, row.crs)
                if not pixel_footprints_match(
                    source.crs, source.transform, spec.crs, spec.transform
                ):
                    continue
                compatible_sources += 1
                # Both contracts are north-up 10 m, so matching footprints mean
                # an integer translation. Read those source pixels directly.
                offset = ~source.transform * spec.transform
                source_window = Window(
                    int(round(offset.c)) + window.col_off,
                    int(round(offset.f)) + window.row_off,
                    window.width, window.height,
                )
                values = _decode(source.read(
                    window=source_window, boundless=True, masked=True,
                ))
                valid = np.isfinite(values).all(axis=0) & ~np.isfinite(output).all(axis=0)
                output[:, valid] = values[:, valid]
        if not candidates.empty and not compatible_sources:
            raise NativeAlignmentError(
                "No native AEF source matches the requested CRS and pixel grid; "
                "cross-zone or fractional-pixel resampling is not performed."
            )
        return output

    def sample_points(
        self, coords: Sequence[tuple[float, float]], years: Sequence[int],
        *, crs: str = "EPSG:4326",
    ) -> np.ndarray:
        """Return [N,T,64]; read each touched native window only once per COG."""
        xy = np.asarray(coords, dtype=np.float64)
        if xy.ndim != 2 or xy.shape[1] != 2 or not np.isfinite(xy).all():
            raise ValueError("coords must contain finite [N,2] coordinates")
        output = np.full((len(xy), len(years), 64), np.nan, np.float32)
        if not len(xy):
            return output
        longitude, latitude = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(
            xy[:, 0], xy[:, 1]
        )
        bbox = (min(longitude), min(latitude), max(longitude), max(latitude))
        candidates = self.query(bbox, years)
        for time_index, year in enumerate(years):
            for row in candidates.loc[candidates.year == year].itertuples(index=False):
                with _open_source(row.path) as source:
                    _check_raster(source, row.crs)
                    x, y = Transformer.from_crs(crs, source.crs, always_xy=True).transform(
                        xy[:, 0], xy[:, 1]
                    )
                    columns, rows = (~source.transform) * (np.asarray(x), np.asarray(y))
                    rows, columns = np.floor(rows).astype(int), np.floor(columns).astype(int)
                    selected = np.flatnonzero(
                        (rows >= 0) & (rows < source.height)
                        & (columns >= 0) & (columns < source.width)
                        & ~np.isfinite(output[:, time_index]).all(axis=1)
                    )
                    groups: dict[tuple[int, int], list[int]] = defaultdict(list)
                    chunk_y, chunk_x = (min(256, value) for value in source.block_shapes[0])
                    for index in selected:
                        groups[(rows[index] // chunk_y, columns[index] // chunk_x)].append(index)
                    for (block_y, block_x), indices in groups.items():
                        top, left = block_y * chunk_y, block_x * chunk_x
                        window = Window(
                            left, top, min(chunk_x, source.width - left),
                            min(chunk_y, source.height - top),
                        )
                        values = _decode(source.read(window=window, masked=True))
                        output[indices, time_index] = values[
                            :, rows[indices] - top, columns[indices] - left
                        ].T
        return output


def _source_path(value: str) -> str:
    value = str(value)
    prefix = "gs://alphaearth_foundations/"
    if value.startswith(prefix):
        return PUBLIC_ROOT + value[len(prefix):]
    if value.startswith(PUBLIC_ROOT) or Path(value).is_file():
        return value
    raise ValueError("Index paths must be official GCS objects or existing local COG files")


def _open_source(path: str):
    return rasterio.open(path)


def _check_raster(source, expected_crs: str) -> None:
    if (
        source.count != 64 or set(source.dtypes) != {"int8"} or source.nodata != -128
        or source.crs != rasterio.crs.CRS.from_user_input(expected_crs)
        or not np.allclose((source.transform.a, source.transform.b,
                            source.transform.d, source.transform.e), (10, 0, 0, -10))
    ):
        raise ValueError("COG does not match the official 10 m/64-band int8/NoData/CRS contract")
    if any(source.descriptions) and source.descriptions != BANDS:
        raise ValueError("COG band descriptions must be ordered A00 through A63")


def _decode(encoded: np.ma.MaskedArray) -> np.ndarray:
    values = np.asarray(encoded.data, dtype=np.float32)
    invalid = np.ma.getmaskarray(encoded).any(axis=0) | (values == -128).any(axis=0)
    values = np.sign(values) * np.square(values / np.float32(127.5))
    values[:, invalid] = np.nan
    return values

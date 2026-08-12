"""Authoritative 10 m grid definitions for local AEF Zarr stores."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from affine import Affine
import pandas as pd
from pyproj import CRS, Transformer

from .earth_engine import AEF_RESOLUTION_M

TESSERA_TILE_DEGREES = 0.1


def _snap_bounds(
    bounds: Sequence[float], resolution: float = AEF_RESOLUTION_M
) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = map(float, bounds)
    return (
        math.floor(xmin / resolution) * resolution,
        math.floor(ymin / resolution) * resolution,
        math.ceil(xmax / resolution) * resolution,
        math.ceil(ymax / resolution) * resolution,
    )


@dataclass(frozen=True)
class GridSpec:
    """Complete, immutable contract for one north-up 10 m output grid."""

    scheme: str
    grid_id: str
    crs: str
    transform: Affine
    width: int
    height: int
    bounds: tuple[float, float, float, float]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        crs = CRS.from_user_input(self.crs)
        if not crs.is_projected:
            raise ValueError(f"AEF grids must use a projected metre CRS, got {self.crs}")
        if not crs.axis_info or not all(
            math.isclose(float(axis.unit_conversion_factor or 0.0), 1.0)
            for axis in crs.axis_info[:2]
        ):
            raise ValueError(f"AEF grid CRS axes must use metres, got {self.crs}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Grid width and height must be positive")
        if not math.isclose(self.transform.b, 0.0) or not math.isclose(
            self.transform.d, 0.0
        ):
            raise ValueError("Rotated or sheared grids are not supported")
        if not math.isclose(abs(self.transform.a), AEF_RESOLUTION_M) or not math.isclose(
            abs(self.transform.e), AEF_RESOLUTION_M
        ):
            raise ValueError(
                f"AEF grid resolution must be {AEF_RESOLUTION_M:g} m; "
                f"got ({self.transform.a}, {self.transform.e})"
            )
        if self.transform.a <= 0 or self.transform.e >= 0:
            raise ValueError("Grid must be north-up with positive x and negative y scale")
        expected_bounds = (
            float(self.transform.c),
            float(self.transform.f + self.height * self.transform.e),
            float(self.transform.c + self.width * self.transform.a),
            float(self.transform.f),
        )
        if not all(
            math.isclose(float(actual), expected, abs_tol=1e-6)
            for actual, expected in zip(self.bounds, expected_bounds)
        ):
            raise ValueError(
                f"Grid bounds do not match transform and shape: "
                f"{self.bounds} != {expected_bounds}"
            )

    @property
    def resolution(self) -> float:
        return AEF_RESOLUTION_M

    def as_dict(self) -> dict:
        return {
            "scheme": self.scheme,
            "grid_id": self.grid_id,
            "tile_id": self.grid_id,
            "crs": self.crs,
            "transform": self.transform,
            "width": self.width,
            "height": self.height,
            "bounds": self.bounds,
            "resolution": self.resolution,
            "metadata": dict(self.metadata),
        }


def _grid_from_projected_bounds(
    *,
    scheme: str,
    grid_id: str,
    crs: str,
    bounds: Sequence[float],
    metadata: Mapping[str, object] | None = None,
) -> GridSpec:
    xmin, ymin, xmax, ymax = _snap_bounds(bounds)
    width = int(round((xmax - xmin) / AEF_RESOLUTION_M))
    height = int(round((ymax - ymin) / AEF_RESOLUTION_M))
    transform = Affine(
        AEF_RESOLUTION_M,
        0.0,
        xmin,
        0.0,
        -AEF_RESOLUTION_M,
        ymax,
    )
    return GridSpec(
        scheme=scheme,
        grid_id=grid_id,
        crs=str(CRS.from_user_input(crs)),
        transform=transform,
        width=width,
        height=height,
        bounds=(xmin, ymin, xmax, ymax),
        metadata=metadata or {},
    )


class ReferenceGridProvider:
    """Read an exact 10 m projected grid from a reference GeoTIFF."""

    def __init__(self, reference: str | Path, grid_id: str):
        self.reference = Path(reference)
        self.grid_id = str(grid_id)

    def get(self) -> GridSpec:
        import rasterio

        with rasterio.open(self.reference) as source:
            if source.crs is None:
                raise ValueError(f"Reference has no CRS: {self.reference}")
            return GridSpec(
                scheme="reference",
                grid_id=self.grid_id,
                crs=str(source.crs),
                transform=source.transform,
                width=int(source.width),
                height=int(source.height),
                bounds=tuple(float(value) for value in source.bounds),
                metadata={"reference": str(self.reference.resolve())},
            )


def tessera_tile_from_world(lon: float, lat: float) -> tuple[float, float]:
    """Return the containing Tessera 0.1-degree tile centre."""
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise ValueError("Coordinates must be finite")
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise ValueError("Coordinates are outside longitude/latitude bounds")
    tile_lon = math.floor(lon * 10.0) / 10.0 + 0.05
    tile_lat = math.floor(lat * 10.0) / 10.0 + 0.05
    return round(tile_lon, 2), round(tile_lat, 2)


def tessera_grid_name(lon: float, lat: float) -> str:
    return f"grid_{lon:.2f}_{lat:.2f}"


def utm_epsg_for_lonlat(lon: float, lat: float) -> int:
    """Return the standard local UTM EPSG code for one WGS84 coordinate."""
    zone = min(60, max(1, int(math.floor((lon + 180.0) / 6.0)) + 1))
    return (32600 if lat >= 0.0 else 32700) + zone


class Tessera01GridProvider:
    """Build 10 m UTM grids for Tessera-compatible 0.1-degree tile IDs."""

    def get(self, lon: float, lat: float, *, coordinates_are_centres: bool = False) -> GridSpec:
        if coordinates_are_centres:
            centre_lon, centre_lat = round(float(lon), 2), round(float(lat), 2)
            expected = tessera_tile_from_world(centre_lon, centre_lat)
            if expected != (centre_lon, centre_lat):
                raise ValueError(
                    "Tessera tile centres must lie on the 0.05-degree offset grid"
                )
        else:
            centre_lon, centre_lat = tessera_tile_from_world(float(lon), float(lat))
        west, east = centre_lon - 0.05, centre_lon + 0.05
        south, north = centre_lat - 0.05, centre_lat + 0.05
        epsg = utm_epsg_for_lonlat(centre_lon, centre_lat)
        transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
        xs, ys = transformer.transform(
            [west, east, west, east], [south, south, north, north]
        )
        return _grid_from_projected_bounds(
            scheme="tessera_0p1",
            grid_id=tessera_grid_name(centre_lon, centre_lat),
            crs=f"EPSG:{epsg}",
            bounds=(min(xs), min(ys), max(xs), max(ys)),
            metadata={
                "tile_center_lon": centre_lon,
                "tile_center_lat": centre_lat,
                "geographic_bounds": [west, south, east, north],
                "tile_size_degrees": TESSERA_TILE_DEGREES,
            },
        )

    def from_bbox(self, bounds: Sequence[float]) -> list[GridSpec]:
        west, south, east, north = map(float, bounds)
        if not (west < east and south < north):
            raise ValueError("bbox must satisfy west < east and south < north")
        lon_start = math.floor(west * 10.0)
        lon_stop = math.ceil(east * 10.0)
        lat_start = math.floor(south * 10.0)
        lat_stop = math.ceil(north * 10.0)
        grids = []
        for lon_index in range(lon_start, lon_stop):
            for lat_index in range(lat_start, lat_stop):
                grids.append(
                    self.get(
                        lon_index / 10.0 + 0.05,
                        lat_index / 10.0 + 0.05,
                        coordinates_are_centres=True,
                    )
                )
        return sorted(grids, key=lambda grid: grid.grid_id)


class MGRSGridProvider:
    """Build S1-GRiTS-compatible 10 m grids from its packaged MGRS table."""

    REQUIRED_COLUMNS = {"mgrs_tile_id", "utm_epsg", "utm_wkt"}

    def __init__(self, mgrs_parquet: str | Path):
        self.path = Path(mgrs_parquet)
        if not self.path.is_file():
            raise FileNotFoundError(self.path)

    def _rows(self, tile_ids: Sequence[str]) -> pd.DataFrame:
        requested = [str(tile).upper() for tile in tile_ids]
        frame = pd.read_parquet(
            self.path, filters=[("mgrs_tile_id", "in", requested)]
        )
        missing_columns = self.REQUIRED_COLUMNS.difference(frame.columns)
        if missing_columns:
            raise ValueError(f"MGRS table missing columns: {sorted(missing_columns)}")
        available = set(frame.mgrs_tile_id.astype(str))
        missing_tiles = sorted(set(requested).difference(available))
        if missing_tiles:
            raise ValueError(f"MGRS tiles not found in {self.path}: {missing_tiles}")
        return frame

    def get(self, tile_id: str) -> GridSpec:
        return self.get_many([tile_id])[0]

    def get_many(self, tile_ids: Iterable[str]) -> list[GridSpec]:
        requested = [str(tile).upper() for tile in tile_ids]
        if not requested:
            raise ValueError("At least one MGRS tile is required")
        from shapely import wkt

        frame = self._rows(requested).set_index("mgrs_tile_id")
        grids = []
        for tile_id in requested:
            row = frame.loc[tile_id]
            geometry = wkt.loads(str(row.utm_wkt))
            grids.append(
                _grid_from_projected_bounds(
                    scheme="mgrs",
                    grid_id=tile_id,
                    crs=f"EPSG:{int(row.utm_epsg)}",
                    bounds=geometry.bounds,
                    metadata={
                        "mgrs_parquet": str(self.path.resolve()),
                        "mgrs_tile_id": tile_id,
                        "utm_epsg": int(row.utm_epsg),
                    },
                )
            )
        return grids

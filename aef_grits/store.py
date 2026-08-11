"""Local Zarr access for AEF grids, inspired by GeoTessera's store API."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd
from affine import Affine
from pyproj import Transformer
import zarr


class AEFZarr:
    """Open one locally streamed AEF tile and sample it chunk-efficiently."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.group = zarr.open_group(str(self.path), mode="r")
        self.array = self.group["embeddings"]
        self.crs = str(self.group.attrs["crs"])
        self.transform = Affine(*self.group.attrs["transform"])
        self.width = int(self.group.attrs["width"])
        self.height = int(self.group.attrs["height"])
        self.tile_id = str(self.group.attrs.get("tile_id", self.path.stem))
        self.years = [int(value) for value in self.group["time"][:]]
        self.bands = list(self.group.attrs.get("bands", []))
        self._year_index = {year: index for index, year in enumerate(self.years)}

    def __repr__(self) -> str:
        return f"AEFZarr({str(self.path)!r}, tile_id={self.tile_id!r}, years={self.years})"

    def _pixel_coordinates(
        self, coords: Sequence[tuple[float, float]], crs: str
    ) -> tuple[np.ndarray, np.ndarray]:
        xy = np.asarray(coords, dtype=np.float64)
        if xy.ndim != 2 or xy.shape[1] != 2:
            raise ValueError("coords must have shape [N,2]")
        if crs != self.crs:
            transformer = Transformer.from_crs(crs, self.crs, always_xy=True)
            x, y = transformer.transform(xy[:, 0], xy[:, 1])
        else:
            x, y = xy[:, 0], xy[:, 1]
        inverse = ~self.transform
        columns, rows = inverse * (np.asarray(x), np.asarray(y))
        return np.floor(rows).astype(np.int64), np.floor(columns).astype(np.int64)

    def contains_points(
        self, coords: Sequence[tuple[float, float]], crs: str = "EPSG:4326"
    ) -> np.ndarray:
        rows, columns = self._pixel_coordinates(coords, crs)
        return (
            (rows >= 0)
            & (rows < self.height)
            & (columns >= 0)
            & (columns < self.width)
        )

    def sample_points(
        self,
        coords: Sequence[tuple[float, float]],
        *,
        years: Sequence[int] | None = None,
        crs: str = "EPSG:4326",
    ) -> np.ndarray:
        """Return ``[N,T,64]`` values while reading every touched chunk once."""
        requested = self.years if years is None else [int(year) for year in years]
        missing = [year for year in requested if year not in self._year_index]
        if missing:
            raise ValueError(f"Years not available in {self.tile_id}: {missing}")
        time_indices = [self._year_index[year] for year in requested]
        rows, columns = self._pixel_coordinates(coords, crs)
        output = np.full(
            (len(rows), len(time_indices), self.array.shape[1]),
            np.nan,
            dtype=np.float32,
        )
        valid = (
            (rows >= 0)
            & (rows < self.height)
            & (columns >= 0)
            & (columns < self.width)
        )
        chunk_y, chunk_x = int(self.array.chunks[2]), int(self.array.chunks[3])
        groups: dict[tuple[int, int], list[int]] = defaultdict(list)
        for index in np.flatnonzero(valid):
            groups[(int(rows[index] // chunk_y), int(columns[index] // chunk_x))].append(
                int(index)
            )
        for (chunk_row, chunk_column), point_indices in groups.items():
            row0, column0 = chunk_row * chunk_y, chunk_column * chunk_x
            row1 = min(self.height, row0 + chunk_y)
            column1 = min(self.width, column0 + chunk_x)
            local_rows = rows[point_indices] - row0
            local_columns = columns[point_indices] - column0
            for output_time, source_time in enumerate(time_indices):
                block = np.asarray(
                    self.array[source_time, :, row0:row1, column0:column1],
                    dtype=np.float32,
                )
                output[point_indices, output_time, :] = block[
                    :, local_rows, local_columns
                ].T
        return output

    def sample_at(
        self,
        x: float,
        y: float,
        year: int,
        *,
        crs: str = "EPSG:4326",
    ) -> np.ndarray:
        return self.sample_points([(x, y)], years=[year], crs=crs)[0, 0]

    def open_xarray(self, *, chunks="auto"):
        """Open the local cube lazily as an xarray Dataset."""
        import xarray as xr

        return xr.open_zarr(str(self.path), consolidated=True, chunks=chunks)


class AEFCatalog:
    """Route point requests across multiple local AEF Zarr tile stores."""

    def __init__(self, catalog_path: str | Path):
        self.path = Path(catalog_path)
        frame = pd.read_parquet(self.path)
        frame = frame.loc[frame.status.eq("complete")].copy()
        if frame.empty:
            raise ValueError(f"No complete stores in {self.path}")
        self.stores: dict[str, AEFZarr] = {}
        for row in frame.sort_values("tile_id").itertuples(index=False):
            path = Path(row.zarr_path)
            if not path.is_absolute():
                path = self.path.parent / path
            self.stores[str(row.tile_id)] = AEFZarr(path)

    def sample_points(
        self,
        coords: Sequence[tuple[float, float]],
        *,
        years: Sequence[int] | None = None,
        crs: str = "EPSG:4326",
        tile_ids: Sequence[str | None] | None = None,
    ) -> np.ndarray:
        first = next(iter(self.stores.values()))
        requested = first.years if years is None else [int(year) for year in years]
        output = np.full(
            (len(coords), len(requested), first.array.shape[1]),
            np.nan,
            dtype=np.float32,
        )
        assignments: dict[str, list[int]] = defaultdict(list)
        if tile_ids is not None:
            if len(tile_ids) != len(coords):
                raise ValueError("tile_ids must have the same length as coords")
            for index, tile_id in enumerate(tile_ids):
                if tile_id is not None and str(tile_id) in self.stores:
                    assignments[str(tile_id)].append(index)
        else:
            unresolved = np.ones(len(coords), dtype=bool)
            for tile_id, store in self.stores.items():
                indices = np.flatnonzero(unresolved)
                if not len(indices):
                    break
                local_coords = [coords[index] for index in indices]
                inside = store.contains_points(local_coords, crs=crs)
                selected = indices[inside]
                assignments[tile_id].extend(selected.tolist())
                unresolved[selected] = False
        for tile_id, indices in assignments.items():
            values = self.stores[tile_id].sample_points(
                [coords[index] for index in indices], years=requested, crs=crs
            )
            output[indices] = values
        return output

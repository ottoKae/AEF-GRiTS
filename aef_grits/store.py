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
        self.grid_id = str(self.group.attrs.get("grid_id", self.tile_id))
        self.scheme = str(self.group.attrs.get("aef:grid_scheme", "reference"))
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

    def _time_indices(self, years: Sequence[int] | None) -> tuple[list[int], list[int]]:
        requested = self.years if years is None else [int(year) for year in years]
        missing = [year for year in requested if year not in self._year_index]
        if missing:
            raise ValueError(f"Years not available in {self.grid_id}: {missing}")
        return requested, [self._year_index[year] for year in requested]

    def read_window(
        self,
        row_start: int,
        row_stop: int,
        column_start: int,
        column_stop: int,
        *,
        years: Sequence[int] | None = None,
    ) -> np.ndarray:
        """Read a clipped pixel window as ``[T,64,H,W]``."""
        row_start = min(self.height, max(0, int(row_start)))
        column_start = min(self.width, max(0, int(column_start)))
        row_stop = min(self.height, int(row_stop))
        column_stop = min(self.width, int(column_stop))
        if row_stop < row_start or column_stop < column_start:
            raise ValueError("Window stop must not precede its start")
        _, time_indices = self._time_indices(years)
        values = [
            np.asarray(
                self.array[index, :, row_start:row_stop, column_start:column_stop],
                dtype=np.float32,
            )
            for index in time_indices
        ]
        if not values:
            return np.empty(
                (0, self.array.shape[1], row_stop - row_start, column_stop - column_start),
                dtype=np.float32,
            )
        return np.stack(values)

    def read_bbox(
        self,
        bounds: Sequence[float],
        *,
        years: Sequence[int] | None = None,
        crs: str = "EPSG:4326",
    ) -> tuple[np.ndarray, Affine]:
        """Read the clipped rectangular window intersecting ``bounds``.

        Returns the values as ``[T,64,H,W]`` and the affine transform of the
        returned window. The four input corners are transformed before the
        pixel envelope is calculated.
        """
        xmin, ymin, xmax, ymax = map(float, bounds)
        if not (xmin < xmax and ymin < ymax):
            raise ValueError("bounds must satisfy xmin < xmax and ymin < ymax")
        xs = np.asarray([xmin, xmax, xmin, xmax], dtype=np.float64)
        ys = np.asarray([ymin, ymin, ymax, ymax], dtype=np.float64)
        if crs != self.crs:
            transformer = Transformer.from_crs(crs, self.crs, always_xy=True)
            xs, ys = transformer.transform(xs, ys)
        inverse = ~self.transform
        columns, rows = inverse * (xs, ys)
        row_start = max(0, int(np.floor(np.min(rows))))
        row_stop = min(self.height, int(np.ceil(np.max(rows))))
        column_start = max(0, int(np.floor(np.min(columns))))
        column_stop = min(self.width, int(np.ceil(np.max(columns))))
        row_stop = max(row_start, row_stop)
        column_stop = max(column_start, column_stop)
        values = self.read_window(
            row_start,
            row_stop,
            column_start,
            column_stop,
            years=years,
        )
        window_transform = self.transform * Affine.translation(column_start, row_start)
        return values, window_transform

    def sample_at(
        self,
        x: float,
        y: float,
        year: int,
        *,
        crs: str = "EPSG:4326",
    ) -> np.ndarray:
        return self.sample_points([(x, y)], years=[year], crs=crs)[0, 0]

    def sample_patches(
        self,
        coords: Sequence[tuple[float, float]],
        *,
        patch_size: int,
        years: Sequence[int] | None = None,
        crs: str = "EPSG:4326",
    ) -> np.ndarray:
        """Return centred patches as ``[N,T,64,P,P]`` without interpolation.

        ``patch_size`` must be odd so every patch has one unambiguous centre
        pixel. Portions outside the store are padded with NaN. Call this method
        in bounded batches when preparing training or validation tensors.
        """
        patch_size = int(patch_size)
        if patch_size <= 0 or patch_size % 2 != 1:
            raise ValueError("patch_size must be a positive odd integer")
        requested, _ = self._time_indices(years)
        rows, columns = self._pixel_coordinates(coords, crs)
        output = np.full(
            (
                len(rows),
                len(requested),
                self.array.shape[1],
                patch_size,
                patch_size,
            ),
            np.nan,
            dtype=np.float32,
        )
        radius = patch_size // 2
        for index, (row, column) in enumerate(zip(rows, columns)):
            source_row0 = max(0, int(row) - radius)
            source_row1 = min(self.height, int(row) + radius + 1)
            source_col0 = max(0, int(column) - radius)
            source_col1 = min(self.width, int(column) + radius + 1)
            if source_row0 >= source_row1 or source_col0 >= source_col1:
                continue
            destination_row0 = source_row0 - (int(row) - radius)
            destination_col0 = source_col0 - (int(column) - radius)
            values = self.read_window(
                source_row0,
                source_row1,
                source_col0,
                source_col1,
                years=requested,
            )
            output[
                index,
                :,
                :,
                destination_row0 : destination_row0 + values.shape[-2],
                destination_col0 : destination_col0 + values.shape[-1],
            ] = values
        return output

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
        id_column = "grid_id" if "grid_id" in frame.columns else "tile_id"
        for row in frame.sort_values(id_column).itertuples(index=False):
            path = Path(row.zarr_path)
            if not path.is_absolute():
                path = self.path.parent / path
            grid_id = str(getattr(row, id_column))
            if grid_id in self.stores:
                raise ValueError(f"Duplicate complete grid_id in catalog: {grid_id}")
            self.stores[grid_id] = AEFZarr(path)

    def open_tile(self, grid_id: str) -> AEFZarr:
        """Open one catalogued grid by its reference, Tessera or MGRS ID."""
        try:
            return self.stores[str(grid_id)]
        except KeyError as exc:
            raise KeyError(f"Unknown AEF grid_id: {grid_id}") from exc

    def read_window(self, grid_id: str, *args, **kwargs) -> np.ndarray:
        return self.open_tile(grid_id).read_window(*args, **kwargs)

    def read_bbox(
        self,
        bounds: Sequence[float],
        *,
        years: Sequence[int] | None = None,
        crs: str = "EPSG:4326",
        grid_ids: Sequence[str] | None = None,
    ) -> dict[str, tuple[np.ndarray, Affine, str]]:
        """Read intersecting windows from each selected store without reprojection."""
        selected = self.stores if grid_ids is None else {
            str(grid_id): self.open_tile(str(grid_id)) for grid_id in grid_ids
        }
        output = {}
        for grid_id, store in selected.items():
            values, transform = store.read_bbox(bounds, years=years, crs=crs)
            if values.shape[-2] and values.shape[-1]:
                output[grid_id] = (values, transform, store.crs)
        return output

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
                if tile_id is not None:
                    tile_id = str(tile_id)
                    self.open_tile(tile_id)
                    assignments[tile_id].append(index)
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

    def sample_patches(
        self,
        coords: Sequence[tuple[float, float]],
        *,
        patch_size: int,
        years: Sequence[int] | None = None,
        crs: str = "EPSG:4326",
        grid_ids: Sequence[str | None] | None = None,
    ) -> np.ndarray:
        """Route centred training/validation patches across catalogued stores."""
        patch_size = int(patch_size)
        if patch_size <= 0 or patch_size % 2 != 1:
            raise ValueError("patch_size must be a positive odd integer")
        first = next(iter(self.stores.values()))
        requested = first.years if years is None else [int(year) for year in years]
        output = np.full(
            (len(coords), len(requested), first.array.shape[1], patch_size, patch_size),
            np.nan,
            dtype=np.float32,
        )
        assignments: dict[str, list[int]] = defaultdict(list)
        if grid_ids is not None:
            if len(grid_ids) != len(coords):
                raise ValueError("grid_ids must have the same length as coords")
            for index, grid_id in enumerate(grid_ids):
                if grid_id is not None:
                    grid_id = str(grid_id)
                    self.open_tile(grid_id)
                    assignments[grid_id].append(index)
        else:
            unresolved = np.ones(len(coords), dtype=bool)
            for grid_id, store in self.stores.items():
                indices = np.flatnonzero(unresolved)
                if not len(indices):
                    break
                inside = store.contains_points(
                    [coords[index] for index in indices], crs=crs
                )
                selected = indices[inside]
                assignments[grid_id].extend(selected.tolist())
                unresolved[selected] = False
        for grid_id, indices in assignments.items():
            output[indices] = self.stores[grid_id].sample_patches(
                [coords[index] for index in indices],
                patch_size=patch_size,
                years=requested,
                crs=crs,
            )
        return output

"""Source-grid validation shared by native AEF point and raster downloads."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable, Iterable

from affine import Affine
import numpy as np
from pyproj import CRS

from .earth_engine import AEF_BANDS, DATASET


NATIVE_PROTOCOL = "native-utm-v1"
SOURCE_PAGE_SIZE = 32


class NativeAlignmentError(ValueError):
    """No native source can satisfy the requested pixel-grid contract."""


@dataclass(frozen=True)
class NativeSource:
    id: str
    year: int
    crs: str
    transform: Affine
    width: int
    height: int
    version: int | str | None
    properties: dict

    @classmethod
    def from_info(cls, info: dict, year: int) -> "NativeSource":
        if info.get("version") is None:
            raise NativeAlignmentError("AEF source metadata lacks an asset version for reproducible resume")
        bands = info.get("bands", [])
        if [band.get("id") for band in bands] != list(AEF_BANDS):
            raise NativeAlignmentError("AEF source must contain ordered bands A00 through A63")
        first = bands[0]
        crs = CRS.from_user_input(first["crs"])
        epsg = crs.to_epsg()
        if epsg is None or not (32601 <= epsg <= 32660 or 32701 <= epsg <= 32760):
            raise NativeAlignmentError(f"AEF source is not a WGS84 UTM grid: {crs}")
        transform = Affine(*first["crs_transform"])
        dimensions = first["dimensions"]
        basis = np.asarray([[transform.a, transform.b], [transform.d, transform.e]]) / 10
        if not _signed_permutation(basis) or len(dimensions) != 2 or min(dimensions) <= 0:
            raise NativeAlignmentError("AEF source is not a valid native 10 m pixel grid")
        if any(band["crs"] != first["crs"]
               or band["crs_transform"] != first["crs_transform"]
               or band["dimensions"] != dimensions for band in bands):
            raise NativeAlignmentError("All 64 AEF bands must share the same source grid")
        properties = info.get("properties", {})
        return cls(str(info["id"]), int(year), crs.to_string(), transform,
                   int(dimensions[0]), int(dimensions[1]), info.get("version"),
                   {key: properties[key] for key in (
                       "MODEL_VERSION", "DATASET_VERSION", "PROCESSING_SOFTWARE_VERSION",
                       "system:time_start", "system:time_end", "UTM_ZONE",
                   ) if key in properties})

    def as_dict(self) -> dict:
        return {"id": self.id, "year": self.year, "crs": self.crs,
                "transform": list(self.transform)[:6], "width": self.width,
                "height": self.height, "version": self.version, "properties": self.properties}

    def image(self, ee):
        image = ee.Image(self.id).select(list(AEF_BANDS)).toFloat()
        # A vector must come from one asset, never a mixture of per-band fallbacks.
        return image.updateMask(image.mask().reduce(ee.Reducer.min()))


def _signed_permutation(matrix: np.ndarray) -> bool:
    rounded = np.rint(matrix)
    return bool(np.allclose(matrix, rounded, rtol=0, atol=1e-10)
                and np.equal(np.abs(rounded).sum(axis=0), 1).all()
                and np.equal(np.abs(rounded).sum(axis=1), 1).all())


def pixel_footprints_match(source_crs, source_transform, target_crs, target_transform) -> bool:
    if CRS.from_user_input(source_crs) != CRS.from_user_input(target_crs):
        return False
    relation = ~source_transform * target_transform
    linear = np.asarray([[relation.a, relation.b], [relation.d, relation.e]])
    translation = np.asarray([relation.c, relation.f])
    return bool(_signed_permutation(linear)
                and (np.abs(translation - np.rint(translation)) < 1e-6).all())


def iter_native_sources(ee, year: int, geometry, request: Callable):
    """Read bounded metadata pages through the caller's retry and token policy."""
    collection = (ee.ImageCollection(DATASET)
                  .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
                  .filterBounds(geometry).select(list(AEF_BANDS)).sort("system:index"))
    offset = 0
    while True:
        page = request(f"native_sources:{year}:{offset}",
                       lambda: collection.toList(SOURCE_PAGE_SIZE, offset).getInfo())
        for info in page:
            yield NativeSource.from_info(info, year)
        if len(page) < SOURCE_PAGE_SIZE:
            break
        offset += SOURCE_PAGE_SIZE


def source_manifest(sources: Iterable[NativeSource]) -> dict:
    payload = {"protocol": NATIVE_PROTOCOL, "vector_dtype": "float32",
               "priority": "ascending_asset_id_first_complete_vector",
               "sources": [source.as_dict() for source in sorted(sources, key=lambda item: (item.year, item.id))]}
    return {**payload, "signature": manifest_signature(payload)}


def manifest_signature(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def native_grid_image(ee, sources: Iterable[NativeSource], grid_crs, grid_transform):
    """Build an explicitly projected mosaic using compatible source pixels only."""
    selected, excluded = [], []
    for source in sorted(sources, key=lambda item: item.id):
        if pixel_footprints_match(source.crs, source.transform, grid_crs, grid_transform):
            selected.append(source)
        else:
            excluded.append({**source.as_dict(), "reason": "incompatible_crs_or_pixel_grid"})
    if not selected:
        raise NativeAlignmentError(
            "No native AEF source matches the requested CRS and pixel grid; "
            "use a compatible UTM reference/grid. Cross-zone resampling is not performed."
        )
    payload = {**source_manifest(selected), "excluded_sources": excluded,
               "target": {"crs": str(grid_crs), "transform": list(grid_transform)[:6]}}
    payload.pop("signature")
    payload["signature"] = manifest_signature(payload)
    # EE mosaic gives the final image priority; reverse to keep the first asset ID.
    image = ee.ImageCollection.fromImages([source.image(ee) for source in reversed(selected)]).mosaic()
    return image.setDefaultProjection(ee.Projection(str(grid_crs), list(grid_transform)[:6])), payload

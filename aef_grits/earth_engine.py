"""Shared Earth Engine expressions for direct AEF retrieval."""

from __future__ import annotations

from collections.abc import Sequence


DATASET = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
AEF_BANDS = tuple(f"A{index:02d}" for index in range(64))
HIGH_VOLUME_URL = "https://earthengine-highvolume.googleapis.com"


def initialize(project: str, *, high_volume: bool = False):
    """Initialize Earth Engine and return its imported module."""
    import ee

    kwargs = {"project": project}
    if high_volume:
        kwargs["opt_url"] = HIGH_VOLUME_URL
    ee.Initialize(**kwargs)
    return ee


def annual_image(year: int, bounds=None, *, renamed: bool = False):
    """Build one annual 64-band AEF mosaic."""
    import ee

    collection = (
        ee.ImageCollection(DATASET)
        .filterDate(f"{int(year)}-01-01", f"{int(year) + 1}-01-01")
        .select(list(AEF_BANDS))
    )
    if bounds is not None:
        collection = collection.filterBounds(bounds)
    image = collection.mosaic().toFloat()
    if renamed:
        image = image.rename(
            [f"aef{int(year)}_center_{band}" for band in AEF_BANDS]
        )
    return image


def multiyear_image(years: Sequence[int], bounds=None):
    """Concatenate annual mosaics into one uniquely named wide image."""
    import ee

    normalized = sorted(set(int(year) for year in years))
    if not normalized:
        raise ValueError("At least one year is required")
    return ee.Image.cat(
        [annual_image(year, bounds, renamed=True) for year in normalized]
    )


def feature_columns(years: Sequence[int]) -> list[str]:
    return [
        f"aef{int(year)}_center_{band}"
        for year in sorted(set(int(value) for value in years))
        for band in AEF_BANDS
    ]

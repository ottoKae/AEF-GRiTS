"""Small Earth Engine boundary doubles for offline download integration tests."""

from types import SimpleNamespace

import pytest

from aef_grits.native_grid import NativeSource
import scripts.stream_aef_grid_ee as grid_stream


@pytest.fixture
def native_grid_sources(monkeypatch):
    def configure(ee, grid, years, image_factory):
        sources = {
            year: [NativeSource(
                id=f"test/native/{year}", year=year, crs=grid["crs"],
                transform=grid["transform"], width=grid["width"], height=grid["height"],
                version=1, properties={},
            )]
            for year in years
        }

        def image(source, _ee):
            result = image_factory(source.year)
            result.setDefaultProjection = lambda projection: result
            return result

        monkeypatch.setattr(NativeSource, "image", image)
        monkeypatch.setattr(grid_stream, "iter_native_sources", lambda ee, year, bounds, request: iter(sources[year]))
        ee.Projection = lambda *args: args
        ee.ImageCollection = SimpleNamespace(
            fromImages=lambda images: SimpleNamespace(mosaic=lambda: images[-1])
        )
        return sources

    return configure

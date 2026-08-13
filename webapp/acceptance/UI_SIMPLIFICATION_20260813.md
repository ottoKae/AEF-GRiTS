# Simplified web workflow acceptance — 2026-08-13

The browser workflow was reduced to three visible steps:

1. choose Points or Grid;
2. choose the relevant input and spatial scope;
3. choose one or more years and an output subdirectory, then press Download.

## Visible choices

- Point input: CSV or complete Shapefile component set.
- Grid type: MGRS or Tessera 0.1 degree.
- Grid AOI: server-inspected WGS84 WKT, Shapefile/ZIP with `.prj`, or direct map drawing.
- Years: 2017-2025 multi-select.
- Output: one relative subdirectory under the configured web output root.

Reference grids, retry counts, block size, checkpoint intervals and other
technical settings were removed from the primary browser form. The underlying
CLI/API still supports Reference grids. Safe tested defaults and the signed
preflight-confirm protocol remain active behind the single Download action.

## Validation behavior

- CSV requires unique `sample_id` plus finite WGS84 `lon` and `lat`.
- Point Shapefile requires declared CRS and only Point/MultiPoint geometry.
  Polygon Shapefiles are rejected rather than silently converted.
- AOI Shapefile CRS is read from `.prj` and transformed server-side to
  EPSG:4326 for lookup/display.
- WKT is explicitly interpreted as EPSG:4326 because WKT geometry text carries
  no reliable CRS declaration.
- AOIs must be valid Polygon/MultiPolygon geometry in WGS84 bounds.

## Acceptance

- Full test suite: **56 passed**.
- Browser workflows: CSV points, Point Shapefile, WKT-to-Tessera and
  WKT-to-authoritative-MGRS all passed.
- Polygon point-input rejection and WKT AOI inspection have dedicated API/unit
  coverage.
- `git diff --check` and Python compilation passed.
- Screenshot: [simplified_ui_20260813.png](simplified_ui_20260813.png).

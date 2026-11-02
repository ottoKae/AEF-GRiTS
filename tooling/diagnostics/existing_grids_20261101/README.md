# Existing AEF raster audit

Inspect existing deliveries without modifying them. Install the repository's
download dependencies; run from its root. Reports and small source checks belong
in a new data/audit batch outside the checkout.

## Inventory by product and year

```bash
python -m tooling.diagnostics.existing_grids_20261101.inventory --roots DATA_ROOT OTHER_DATA_ROOT --out NEW_REPORT_DIR
```

Outputs: `inventory.csv`, `inventory.parquet`, `discovery.json`. The scanner reads
raster headers and Zarr time coordinates, prunes Zarr chunk directories and
deduplicates filesystem aliases. Missing metadata and access failures stay in
the discovery log; they are not counted as empty or passing products.

`full_mgrs` requires agreement with the bundled authoritative MGRS CRS, transform
and dimensions. A tile-like filename or `scheme=mgrs` is insufficient. Separate
compression variants and TIFF/Zarr companions remain separate products; row counts
are not counts of unique geographic tiles. A 10 m header or native-protocol tag
alone does not prove all pixel values or source masks are correct.

## Bounded native-source spot checks

Supply a JSON list of explicit windows:

```json
[{"id":"case_1","path":"ABSOLUTE_STORE.zarr","year":2025,"row":100,"column":100}]
```

```bash
python -m tooling.diagnostics.existing_grids_20261101.spotcheck --cases CASES.json --out NEW_CHECK_DIR --high-volume
```

Each window is 16×16 with all 64 bands. Existing credentials are reused without
login. The existing audit request runner limits the batch to 60 attempts, a
60-second request deadline and one retry; initialization is not included in this
API-attempt deadline. `--high-volume` selects the existing Earth Engine endpoint
alternative, not a different data source or identity.

The tool compares local values with compatible native sources, and checks any
cross-UTM candidates on the target grid. Missing values are not a pass, unexplained
differences are not automatically called resampling, and a passing window is not
whole-tile certification. Never extend a cached check directory to a different
set of case IDs/inputs. No whole-grid remediation is performed by these helpers.

# Data organization

Generated data are intentionally excluded from Git. The recommended local tree is:

```text
data/
├── source/                         # plantation inventory, LCLU and reference rasters
└── drive_exports/                  # optional manually downloaded files

outputs/
├── catalog.parquet                  # local Zarr tile index
├── point_stream/
│   ├── catalog.parquet              # point-shard index
│   ├── run.json
│   ├── report.json
│   └── shards/part*.parquet
├── patches/
│   ├── catalog.parquet
│   ├── report.json
│   ├── shards/part*.npy             # [N,T,64,P,P] float32
│   └── metadata/part*.parquet       # IDs, labels, split and patch QC
├── reference/<grid_id>/             # arbitrary validated 10 m reference grid
├── tessera_0p1/
│   └── grid_<lon>_<lat>/            # one 10 m UTM Zarr per 0.1-degree cell
├── mgrs/
│   ├── catalog.parquet
│   ├── run_summary.json
│   └── <mgrs_tile_id>/
│       ├── aef_<mgrs_tile_id>_2017_2025.zarr/
│       ├── aef_<mgrs_tile_id>_2017_2025.zarr.progress.json
│       └── aef_<mgrs_tile_id>_2017_2025.zarr.report.json
├── plantation_inventory/
│   ├── plantation_polygon_inventory.csv
│   ├── plantation_polygon_aef_points_all.csv
│   ├── plantation_polygon_aef_points_gee_minimal.csv
│   └── plantation_polygon_sampling_report.json
├── submitted_tasks/                # Earth Engine task manifests
├── raw_exports/                    # chunked point CSVs from Google Drive
├── point_features/                 # validated merged point tables
├── polygon_features/               # annual polygon prototypes
├── global_background_2025/
│   ├── global_background_points_2025.csv
│   ├── raw_exports/
│   └── features/
└── rasters/<tile_id>/
    ├── <prefix>_<year>_64band_10m*.tif
    └── vrt/
```

## Point table contracts

The minimal Earth Engine input contains:

```text
sample_id, lon, lat
```

The input may be CSV or Parquet, has one row per point, and uses WGS84
longitude/latitude. Shapefile, GeoPackage, GeoJSON and GeoParquet sources pass
through the same canonical conversion: point geometries are retained; polygons
become one interior representative point or reference-grid-aligned internal pixel
centres. Direct point sampling is fixed at 10 m. Direct outputs are Parquet shards
rather than Zarr stores.

The wide export contains one identifier followed by 64 features per requested year:

```text
sample_id,
aef2017_center_A00 ... aef2017_center_A63,
...,
aef2025_center_A00 ... aef2025_center_A63
```

The master registry may additionally contain `polygon_id`, species fields, MGRS tile,
province, projected coordinates and `sample_weight`. The merge step joins these fields
back by `sample_id`.

## Dense-store contract and model roles

Complete grids use Zarr v3 with `float32` embeddings, chunks
`(1,64,64,64)`, shards `(1,64,512,512)`, and lossless Blosc Zstd level 7 with
no shuffle and `typesize=4`. The compression protocol is versioned in the store
attributes, catalog and run signature.

- Complete MGRS tile Zarr stores are the authoritative source for wall-to-wall
  inference and repeated spatial queries.
- Point Parquet shards are the preferred source for pixel/centre training and
  spatially independent validation.
- Spatial model inputs are sampled on demand from Zarr with
  `AEFZarr.sample_patches()`, which returns `[N,T,64,P,P]`. Patch files need not
  duplicate the complete tile unless a separate immutable training delivery is
  operationally required.

## Polygon prototypes

Every valid point vector is L2-normalized. Point directions are averaged using the
stored sample weights and the resulting polygon mean is normalized again. Therefore,
each polygon has one 64-D unit-sphere prototype per year and large polygons do not gain
extra weight merely because they contain more sampled points.

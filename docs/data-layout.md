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
├── <tile_id>/aef/zarr/
│   ├── aef_<tile_id>_2017_2025.zarr/
│   ├── aef_<tile_id>_2017_2025.zarr.progress.json
│   └── aef_<tile_id>_2017_2025.zarr.report.json
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
    ├── <prefix>_<year>_64band_30m*.tif
    └── vrt/
```

## Point table contracts

The minimal Earth Engine input contains:

```text
sample_id, lon, lat
```

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

## Polygon prototypes

Every valid point vector is L2-normalized. Point directions are averaged using the
stored sample weights and the resulting polygon mean is normalized again. Therefore,
each polygon has one 64-D unit-sphere prototype per year and large polygons do not gain
extra weight merely because they contain more sampled points.

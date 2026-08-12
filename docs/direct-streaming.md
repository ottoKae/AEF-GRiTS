# Direct Earth Engine streaming design

## Why this differs from a public cloud Zarr store

GeoTessera exposes a prebuilt public Zarr object store. A client can issue HTTP
range requests for existing chunks, so point and region reads are true remote Zarr
reads. AlphaEarth Foundation embeddings are exposed through Earth Engine rather
than a public Zarr endpoint. AEF-GRiTS therefore asks Earth Engine to compute small
tables or pixel blocks and immediately commits each response into a local
Parquet/Zarr layout.

## Point path

```text
sample registry CSV/Parquet or converted vector geometry
        ↓ fixed row chunks
ee.data.computeFeatures
        ↓ Pandas DataFrame response
atomic Parquet shard
        ↓
catalog.parquet + QC report
```

Point rows are left-joined back to the original registry by `sample_id`. Masked or
missing points remain explicit rows with NaN features instead of silently
disappearing. A run signature binds the coordinates, years, scale and AEF asset to
the output directory.

All supported vector formats pass through the same deterministic conversion and
preflight layer. `--validate-only` stops before Earth Engine initialization, so
schema, coordinate, duplicate and year failures do not consume quota.

## Grid path

```text
10 m grid provider (reference, Tessera 0.1-degree, or S1-GRiTS MGRS)
        ↓ 256 x 256 request windows
ee.data.computePixels (64 float32 bands)
        ↓ NumPy structured array
single-writer block commit
        ↓
Zarr v3 embeddings(time,band,y,x)
        ↓ lossless Blosc Zstd-7, no shuffle
        ↓
progress ledger + Parquet catalog
```

Earth Engine limits each `computePixels` response to 48 MB uncompressed. The
default 256-pixel block uses about 16.8 MB. Fetches may run concurrently, but all
Zarr writes occur in the main process so two requests never mutate the same shard.

All three providers emit the same immutable grid contract and the same
`embeddings(time,band,y,x)` layout. AEF point and grid extraction are fixed at
10 m; any coarser comparison is a separate downstream product.

The default full-store codec was selected from a bit-exact comparison across
17MNT, 17MPU, 17MPV and 17NQA for 2017, 2021 and 2025. Zstd level 7 without
shuffle occupied 17.9%-19.8% of raw float32 bytes and reduced storage by
63.1%-63.7% relative to the former Zstd-3 byte-shuffle configuration. Tile Zarr
is therefore the inference product; point Parquet and bounded patches read from
Zarr are the training and validation products.

## Relationship to S1-GRiTS and GeoTessera

| Property | S1-GRiTS | GeoTessera | AEF-GRiTS direct stream |
|---|---|---|---|
| Remote source | OPERA/ASF scenes | Public S3 Zarr/NPY | Earth Engine computation |
| Metadata index | Parquet catalog | Parquet manifest | Parquet catalog |
| Local grid | per-track `(time,y,x)` bands | `(time,band,y,x)` | `(time,band,y,x)` |
| Spatial write | chunk-aligned blocks | shard fill | bounded computePixels blocks |
| Point access | catalog to track to pixel | zone to chunk to pixel | catalog to tile to chunk to pixel |
| Resume unit | month/block | file/shard | point shard or grid block |

## When to keep batch export

Interactive requests must finish quickly and are subject to request, aggregation
and concurrency quotas. Keep the existing Drive exporter, or use Earth Engine
Cloud Storage batch export, when a computation repeatedly times out online. Batch
export remains more suitable for very large one-off rasters; direct streaming is
best for point tables, pilots, resumable tiled ingestion and iterative experiments.

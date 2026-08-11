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
sample registry CSV/Parquet
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

## Grid path

```text
reference GeoTIFF grid
        ↓ 256 x 256 request windows
ee.data.computePixels (64 float32 bands)
        ↓ NumPy structured array
single-writer block commit
        ↓
Zarr v3 embeddings(time,band,y,x)
        ↓
progress ledger + Parquet catalog
```

Earth Engine limits each `computePixels` response to 48 MB uncompressed. The
default 256-pixel block uses about 16.8 MB. Fetches may run concurrently, but all
Zarr writes occur in the main process so two requests never mutate the same shard.

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

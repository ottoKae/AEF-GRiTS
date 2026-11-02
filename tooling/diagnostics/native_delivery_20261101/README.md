# Native-delivery live regression

Run from the repository in the download environment with existing credentials.
This is a real, bounded Earth Engine test, not an offline test or a full-tile job.

```powershell
$env:PYTHONPATH = (Get-Location).Path
python tooling/diagnostics/native_delivery_20261101/verify.py --audit PATH_TO_FROZEN_NATIVE_AUDIT --out NEW_DATA_BATCH
```

The audit directory is produced by `../native_alignment_20261101/` and contains
historical coordinates, captured source-grid arrays and metadata. It is read-only.
Choose a new output directory under the project's data store, not the repository.

Checks:

- The actual point CLI: four points in 2017/2025, chunk sizes 1 and 4, native-2025
  equality, source metadata and completed resume.
- The actual grid CLI: five historical 16×16 grid/year windows, two UTM-boundary
  windows, Zarr/GeoTIFF equivalence and completed TIFF-only recovery.
- A reference shifted by 3 m must fail without creating a Zarr product.

Each subprocess has a five-minute outer limit; API attempts have a 60-second
deadline with one retry. No browser login, remote task submission, large raster
download or historical-data overwrite is performed. Logs, reference TIFFs and
`verification.json` stay with the new data batch. A failed check is not a pass;
inspect the corresponding log before resuming.

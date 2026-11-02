# Bounded native-grid audit

These are one-off live diagnostic helpers, not a replacement downloader. They read historical products without modifying them, reuse existing Earth Engine credentials and save small new arrays and provenance under a separately supplied embedding-data directory.

Run from the repository using the environment already used for AEF-GRiTS:

```text
python -m tooling.diagnostics.native_alignment_20261101.audit --archive AEF_GRITS_ARCHIVE --points FOUR_POINT_PARQUET --out NEW_AUDIT_DIRECTORY
python -m tooling.diagnostics.native_alignment_20261101.finish_audit --root NEW_AUDIT_DIRECTORY
```

The first helper is specific to the archived 17MPU/17NQA multi-year probes, the 0.1-degree probe and the four-point 2025 GUI download identified in the report. It does not discover arbitrary datasets. Supply a new directory for a different input, protocol or source-selection experiment.

Both helpers share a maximum of 60 request attempts, including recoverable retries. Authentication initialization and credential refresh are excluded. The requests are sequential, use a 60-second Earth Engine deadline and at most one retry. Raster responses contain only 16 x 16 x 64 values; no full tile is downloaded.

Named cached responses can resume the same frozen experiment. They are **not** a general content-addressed cache: changing inputs or sampling semantics requires a new directory. The second helper adds independent footprint checks and reuses Stage A artifacts without changing them. Its v2 result corrects a metadata field name only; underlying request artifacts are unchanged.

Interpretation:

- `summary.json` uses an identity-basis check for identical array orientation. A false value can simply mean reversed row order.
- `native_footprint_verification_v2.json` allows integer translations and signed axis permutations, then checks actual arrays by integer reindexing. This is the final footprint assessment.
- `status=complete` means execution completed, not that every result passed. Inspect valid pair counts, masks, source counts and differences.
- Comparisons cast both sides to float32, the raster delivery dtype. Dataframe encoding/dtype differences are not counted as spatial differences.
- `.npy` byte totals include file headers; they are not measured HTTP traffic.
- Raw data, metadata, coordinates and private host provenance remain in the supplied data directory. Do not publish that directory or credential/project identifiers.

Tests:

```text
python -m pytest tests/test_native_alignment_audit.py -q -p no:cacheprovider --basetemp UNIQUE_DISPOSABLE_TEST_DIRECTORY
```

The live results and limitations are documented in [the Chinese audit report](../../../docs/native-alignment-audit-20261101.zh-CN.md).

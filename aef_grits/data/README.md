# Packaged MGRS grid index

`mgrs.parquet` is the self-contained global grid resource used by AEF-GRiTS.
It has 19,002 rows and the columns `mgrs_tile_id`, `utm_epsg`, `utm_wkt`, and
WGS84 WKB `geometry`. Its SHA-256 is
`5c1ebd1f234ae74d9c249d91cb9c269eb6defa37a901da0c7bbcb7e50523fc6b`.

The table preserves the 10 m, buffered MGRS grid contract used when AEF-GRiTS
was separated from the author's S1 processing workflow. It is now distributed
inside this package, so users do not install, clone, or locate another repository.
An explicit `--mgrs-index` remains available only to override this resource.

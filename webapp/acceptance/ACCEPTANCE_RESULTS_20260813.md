# Web application acceptance results — 2026-08-13

Project: `<redacted-project>`<br>
Year: `2025`<br>
Server: `http://127.0.0.1:5555`

| Check | Scope | Expected | Observed | Result |
|---|---|---|---|---|
| Point | `single_point_2025.csv`, one WGS84 point | One row and 64 complete AEF columns | 1 row, 64/64 columns complete, 0 incomplete rows, 0.56 s | PASS |
| Tessera | bbox `[-80.04,-1.04,-80.01,-1.01]` | Exactly one 0.1-degree grid | `grid_-80.05_-1.05`, shape `[1,64,1107,1114]`, 1,233,198 valid pixels, 89.12 s | PASS |
| MGRS plan | `anhui_small_aoi.geojson` | Resolve IDs but do not download | `50RMV`, `50RNV`; 57.49 GiB uncompressed estimate; no task started | PASS |
| Reference | 16 x 16, 10 m, EPSG:32717 GeoTIFF | Preserve reference grid exactly | shape `[1,64,16,16]`, 256 valid pixels, 1.01 s | PASS |

Output sizes were approximately 0.05 MiB for the point result, 0.06 MiB for
the reference result, and 67.51 MiB for the losslessly compressed Tessera Zarr.

Task IDs:

- Point: `20260813_160802_750f98`
- Reference: `20260813_160750_042b93`
- Tessera: `20260813_160750_4272f7`

The browser DOM showed all three executed tasks as `done` with 100% progress.
The MGRS workflow was deliberately stopped at the confirmation dialog so that
the two full MGRS tiles were not downloaded.

## Manual browser confirmation

1. Open `http://127.0.0.1:5555` and confirm that the three task cards above are
   visible as `done` and `100%`.
2. Switch to point download, select `single_point_2025.csv`, select 2025, and
   verify the form can be submitted. The completed point task already proves
   the upload-to-Parquet path.
3. For Tessera, enter W=-80.04, E=-80.01, S=-1.04, N=-1.01 and apply the bbox.
   Planning must report one grid: `grid_-80.05_-1.05`.
4. Import `anhui_small_aoi.geojson`, select MGRS, and click plan/submit. The
   confirmation must report two grids, `50RMV` and `50RNV`, and about 57.49 GiB
   uncompressed. Click **Cancel** in that browser confirmation; do not start it.
5. For Reference, enter
   `D:\Project\claude-demo\AEF-GRiTS\outputs\live_probe\reference.tif` and tile
   ID `acceptance_ref_16x16`. Planning must report one grid and 65,536 raw bytes.

The screenshot `completed_tasks.png` records the automated browser rendering
check after the downloads completed.

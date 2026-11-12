# Browser acceptance fixtures

These fixtures exercise the four web workflows without starting a large MGRS
download.

- `anhui_small_aoi.geojson`: an 0.08 by 0.08 degree AOI used only to verify MGRS
  planning. Do not confirm the resulting MGRS download during this acceptance run.

Point CSV/Shapefile and tiny reference-raster fixtures are generated in pytest
temporary directories by `tests/test_webapp_e2e.py`; downloaded or generated
data files are deliberately excluded from the repository.

Only this README and `anhui_small_aoi.geojson` are intentionally public in this
directory. All historical acceptance reports, screenshots, snapshots, local
paths, and internal build artifacts remain on the developer's machine and are
ignored by Git, including root-level records and per-run subdirectories. Publish
reviewed, sanitized reports and selected images in `docs/` and `docs/assets/`.
The downloadable point example is `webapp/static/example-points.csv`; it is an
explicit exception to the repository's CSV ignore rule and is packaged with the GUI.

Use 2025 for every download. The current two-stage acceptance uses output
subdirectories `acceptance_v2/point_2025`, `acceptance_v2/tessera_2025`, and
`acceptance_v2/reference_2025` so the artifacts do not collide with the first
browser smoke test.

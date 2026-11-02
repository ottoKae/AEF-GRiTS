# Native UTM download update / 原生 UTM 下载更新

2026-11-02

## Scope / 范围

`fix/native-utm-alignment` contains the GEE download protocol, its GeoTIFF
delivery dependencies, source-access and coverage checks, tests, and reproducible
small-data audit tools. The unfinished GUI and homepage remain on
`feature/web-workbench`; they are not part of this main-branch update.

本次只更新GEE下载内核与验收工具。网页分支从更新后的内核继续开发，保留原有正式网页，
不把尚未完成的新工作台、首页或安装引导合入 `main`。历史界面截图、运行日志和实际
AEF数据保留本地，不随本次发布上传。

## Changes and compatibility / 更新与兼容性

- `native-utm-v1` selects complete 64-dimensional vectors on explicit native
  UTM 10 m grids and records source asset versions, CRS and pixel identity.
- Dense grids reject cross-UTM filling and fractional pixel offsets. Opposite
  row order is allowed when it preserves exactly the same ground pixels.
- Zarr, optional annual GeoTIFF and TIFF-only deliveries retain source manifests.
  GeoTIFF support is included because the real native-alignment checks exercise it.
- Existing products are not changed. **Use a new batch directory for old-protocol
  downloads; old checkpoints cannot be resumed as native products.**
- The legacy Drive point exporter is planning-only. Use
  `python -m scripts.stream_aef_points_ee` for actual point downloads.
- `auth verify --check-aef` checks collection access explicitly; downloads do not
  start a login flow. Source-mask coverage is not local-product integrity.

旧成果不删除、不自动改写；也不因MGRS编号一致就宣称原生一致。缺测保留NaN。
旧Drive点位接口停止真实导出，旧协议检查点不能直接用新版继续；这两项是兼容性变化，
不是单纯界面优化。

## Evidence / 验收依据

The 2026-11-01 real GEE pilot compared seven 16×16 windows: 1,792 pixels and
114,688 float32 feature values matched their frozen native references exactly.
Four points in 2017/2025, two chunk sizes, Zarr/TIFF output and TIFF-only recovery
were also checked. This is sampled evidence, not global or full-tile certification.

See [implementation and live validation](native-alignment-implementation-20261101.zh-CN.md)
and [the earlier diagnostic report](native-alignment-audit-20261101.zh-CN.md).
Raw samples, credentials and machine-specific paths are not included in Git.

The backend-only tree passed **223 tests with 2 platform skips** on Windows on
2026-11-02. The skips require directory-symlink privileges and POSIX process
groups. The earlier combined GUI/backend tree passed 336 tests with 2 skips;
these are different test inventories, not a claim that the unfinished GUI shipped.

本分支相对发布前 `main` 不修改 `webapp/`、`website/`、网页安装入口或网页CI。
本地测试成功不等于Linux/macOS已经验证，跨平台结果仍以GitHub Actions为准。

本次未重新下载完整MGRS瓦片。大量历史成果的位置和逐瓦片核验仍需另行完成，不阻塞
新下载协议发布，也不能写成“所有旧数据均已验证”。Linux/macOS结果应以本分支CI为准。

# AEF-GRiTS

[English](README.md) | [简体中文](README.zh-CN.md)

AEF-GRiTS 是一套独立且可复现的年度 AlphaEarth Foundation（AEF）嵌入特征采样、导出、下载与验证工作流。它从 `LL0912/DOCC_BALSA` 的生产级人工林制图流程中整理而来，现已不再依赖该仓库。

本工作流支持两类数据产品：

1. **点与地块特征**：在清查地块内部生成确定性采样点，提取年度 64 维 AEF 向量，并计算每个地块、每一年的单位超球面原型。
2. **全覆盖栅格**：按照已有参考格网导出年度 64 波段栅格，并建立 VRT 和数据目录。

Earth Engine 数据源为 `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`，波段为 `A00` 至 `A63`。

## 仓库结构

```text
aef_grits/
├── features.py                     # AEF 数据结构、质量控制与球面运算
├── catalog.py                      # S1-GRiTS 数据目录与格网辅助工具
├── earth_engine.py                 # Earth Engine 影像构建工具
├── atomic.py                       # 原子化 JSON 与 Parquet 写入
└── store.py                        # 本地 Zarr 与多瓦片目录读取器
samples/
├── aef_plantation_polygon_pixels.py
└── aef_balsa_polygon_pixels.py
scripts/
├── stream_aef_points_ee.py         # Earth Engine 点特征直传本地 Parquet 分片
├── stream_aef_grid_ee.py           # Earth Engine 格网直传本地 Zarr v3
├── export_aef_points_ee.py
├── download_drive_exports.py
├── merge_aef_point_exports.py
├── aggregate_polygon_aef.py
├── build_global_background.py
├── validate_background_exports.py
├── export_aef_grid_ee.py
└── build_aef_raster_catalog.py
docs/
├── data-layout.md
├── direct-streaming.md
└── source-map.md
```

下载得到的 CSV、Parquet、GeoTIFF、VRT 和 Zarr 产品均被 Git 忽略。推荐的本地目录结构与表字段约定见 [`docs/data-layout.md`](docs/data-layout.md)。

## 安装

包含 GDAL 栅格目录工具的可复现安装方式为：

```bash
conda env create -f environment.yml
conda activate aef_grits
```

如果只处理点数据，可以执行：

```bash
python -m pip install -e ".[gee,geo,stream,dev]"
```

首次使用时认证 Earth Engine，并指定计费与配额项目：

```bash
earthengine authenticate --auth_mode=localhost --force
earthengine set_project YOUR_GEE_PROJECT
```

如果配额属于其他 Earth Engine 项目，请替换示例项目名。认证信息从 Earth Engine 的标准位置读取，绝不会保存到本仓库。

## 不经过 Google Drive，直接写入本地

以下命令均应在仓库根目录执行。推荐流程使用两个同步 Earth Engine Python 接口，将每次返回的结果直接写入本地磁盘：

- `ee.data.computeFeatures` 用于计算 `FeatureCollection`。设置 `fileFormat="PANDAS_DATAFRAME"` 后，Earth Engine Python 客户端会自动请求全部分页，并返回一个 Pandas DataFrame。AEF-GRiTS 随后将该表以原子方式提交为 Parquet 分片。
- `ee.data.computePixels` 用于计算一个明确定义的像素格网。设置 `fileFormat="NUMPY_NDARRAY"` 后，接口返回 NumPy 结构化数组。AEF-GRiTS 将其中 64 个命名字段转换为 `[64,H,W]` 的 float32 数据块，并写入本地 Zarr。

上述方式属于交互式计算：不会创建批处理任务、Drive 文件夹或人工下载步骤。它们适合点表、小规模试验和边界明确的格网分块。对于无法在 Earth Engine 交互式限制内完成的超大计算，仓库仍保留批处理与 Drive 回退方案。

官方参考资料：

- [Earth Engine `ee.data.computeFeatures`](https://developers.google.com/earth-engine/apidocs/ee-data-computefeatures)
- [Earth Engine `ee.data.computePixels`](https://developers.google.com/earth-engine/apidocs/ee-data-computepixels)
- [`computePixels` REST 限制](https://developers.google.com/earth-engine/reference/rest/v1/projects.image/computePixels)

### 下载前先选择数据产品

| 需求 | 推荐产品 | 命令 |
|---|---|---|
| 训练/验证点或地块原型 | Parquet 点分片 | `stream_aef_points_ee.py` |
| 仅提取稀疏候选像元 | Parquet 点分片 | `stream_aef_points_ee.py` |
| 完整年度地图或后续任意像元查询 | 与参考格网对齐的 Zarr | `stream_aef_grid_ee.py` |
| 交互式请求反复超时 | 批处理导出回退方案 | `export_aef_*_ee.py` |

如果实验只需要数千个点，不要下载完整格网。只有后续分析需要反复访问大量任意像元或制作全覆盖地图时，才应使用 Zarr。

### 定义点位下载

输入文件可以是 CSV 或 Parquet，且必须包含：

```text
sample_id,lon,lat
```

- `sample_id` 必须唯一。
- `lon` 与 `lat` 必须是有限的 WGS84 坐标（`EPSG:4326`）。
- `polygon_id`、树种、瓦片、省份和样本权重等附加字段会保留在每个输出分片中。
- AEF 缺失或被掩膜的样本仍保留对应记录，其特征值为 NaN。

对于人工林清查数据，可先在地块内部创建确定性的采样点：

```bash
python samples/aef_plantation_polygon_pixels.py --shapefile /path/to/plantations.shp --out-dir outputs/plantation_inventory
```

可以通过 `--s1-catalog /path/to/catalog.parquet` 附加包含该点的 S1-GRiTS 格网信息。主要点表为：

```text
outputs/plantation_inventory/plantation_polygon_aef_points_all.csv
```

### 直接提取点特征

```bash
python scripts/stream_aef_points_ee.py --samples outputs/plantation_inventory/plantation_polygon_aef_points_all.csv --out-dir outputs/point_stream --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --chunk-size 1000 --workers 1
```

输出支持断点续传，并按分片组织：

```text
outputs/point_stream/
├── run.json
├── catalog.parquet
├── report.json
└── shards/
    ├── part00001.parquet
    └── ...
```

点采样属于 Earth Engine 聚合计算，建议初始使用一个工作线程。只有代表性计时测试稳定后，才增加至两个工作线程。

`--chunk-size` 控制本地断点续传单元，以及单个 Earth Engine 表达式中包含的坐标数量。`--page-size` 控制服务器响应的内部分页大小，不改变最终 Parquet 的分片方式。九年数据的每一行包含 576 个特征列：

```text
aef2017_center_A00 ... aef2017_center_A63
...
aef2025_center_A00 ... aef2025_center_A63
```

### 定义并下载参考格网

格网下载需要一个参考 GeoTIFF，其元数据构成完整的目标格网约定：

```text
坐标参考系 + 仿射变换 + 宽度 + 高度
```

参考文件通常应使用相同瓦片的第一阶段 S1-GRiTS 候选区或 logit 栅格。这样可以确保像元中心、图像尺寸和各瓦片投影（`EPSG:32717` 或 `EPSG:32617`）完全一致。仅提供外接矩形不足以锁定像元原点。30 米参考栅格会产生 30 米 AEF 输出；10 米参考栅格的空间像元数约为前者的九倍。

实现使用 Earth Engine 默认的最近邻重投影，以保留 AEF 向量的各个分量，不对嵌入维度进行插值。

```bash
python scripts/stream_aef_grid_ee.py --reference /path/to/17MPU_reference.tif --out outputs/17MPU/aef/zarr/aef_17MPU_2017_2025.zarr --catalog outputs/catalog.parquet --tile-id 17MPU --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --block-size 256 --workers 2
```

默认单次请求为 `256 x 256 x 64 x float32`，未压缩数据量约 16.8 MB，明显低于 Earth Engine `computePixels` 的 48 MB 限制。本地数据立方体为：

```text
embeddings(time, band, y, x) float32
chunks=(1, 64, 32, 32)
shards=(1, 64, 512, 512)
```

较小的内部数据块可降低点采样开销，较大的 Zarr v3 分片则能避免生成数百万个小文件。运行期间会持续提交进度账本，因此命令中断后可以使用相同参数继续执行。

运行签名绑定数据源、年份、参考格网、请求分块、内部数据块和分片大小。如果使用不同约定复用同一输出路径，程序会拒绝执行，避免无提示地混合不同格网。

这里需要区分三个尺度：参考 GeoTIFF 决定最终数据范围；`--block-size` 只决定网络请求分块；Zarr 的 `chunks` 和 `shards` 只决定本地存储组织。例如，完整 MGRS 参考图会生成完整 MGRS 瓦片，而一个对齐的 `1024 x 1024` 裁剪参考图只会生成同样大小的测试数据集。

### 读取本地 Zarr

```python
from aef_grits.store import AEFZarr, AEFCatalog

tile = AEFZarr("outputs/17MPU/aef/zarr/aef_17MPU_2017_2025.zarr")
values = tile.sample_points([(-79.30, -1.10)], years=[2024, 2025])
# values.shape == (1, 2, 64)

catalog = AEFCatalog("outputs/catalog.parquet")
values = catalog.sample_points([(-79.30, -1.10), (-80.10, 0.20)], years=[2025])
```

`AEFZarr.sample_points()` 返回 `[N,T,64]`，按照涉及的 `32 x 32` 数据块对点分组，每个数据块只读取一次。超出数据范围的点返回 NaN。`AEFCatalog` 可以把点路由到多个瓦片数据集；若点位于瓦片重叠边缘，应显式传入 `tile_ids`。对于区域级惰性处理，可以使用：

```python
ds = tile.open_xarray()
print(ds.embeddings.dims)  # ('time', 'band', 'y', 'x')
```

### 性能建议

当前机器和 Earth Engine 项目的实测结果为：

- 1,000 个真实点 × 9 年 × 64 维：16.1 秒，全部记录完整。
- 一个 `256 x 256 x 64` 格网块：27.3 秒，压缩后约 10.9 MB。
- 重新运行时，已完成的点分片和格网块可直接从进度记录中复用，无需再次下载。

当前点采样性能足以支持现有样本规模的实验。完整 11 瓦片格网仍需调优。正式生产前，应在单个瓦片上比较 `--block-size` 256 与 384，以及 `--workers` 1、2 和 4。只有在不增加 HTTP 429、超时或聚合错误的情况下提高吞吐量，才保留对应配置。默认不要对点聚合开启高吞吐端点；它主要适合大量简单像素请求。

最大的性能收益来自空间选择，而不是提高并发。如果第二阶段只对第一阶段候选像元进行分类，应将候选像元中心转换为点表，并使用点特征提取器。只有全覆盖可视化、反复任意查询或未来长期复用足以抵消额外传输和存储成本时，才下载完整瓦片数据立方体。

例如，如果第一阶段候选区只占全部瓦片像元的 5%，稀疏点下载只需请求并保存约 5% 的嵌入值，因此理论上可减少约 95% 的 AEF 传输量和特征存储量。由于 Parquet 元数据、请求初始化和数据目录具有固定开销，实际降幅会略低。只有项目需要完整 AEF 空间图、反复查询任意像元、开展全覆盖诊断或在当前候选掩膜之外长期复用数据时，才应下载完整 Zarr。

对于约 `3667 x 3667` 像元的 30 米瓦片，九年未压缩 AEF 数据约为 31 GB。根据试验压缩率，每个瓦片可能约为 19–21 GB，但实际大小取决于数据。全国 11 瓦片下载之前，必须先完成 17MPU 的实际时间与存储试验。

### 运行 11 瓦片格网前必须完成的基准测试

所有测试均应使用固定的 17MPU 参考格网、相同 Earth Engine 项目和相同本地磁盘，并记录总耗时、成功数据块每秒、重试次数、HTTP 429/超时、有效像元数和最终磁盘占用。

1. **点下载基线**：对固定的 10,000 个点下载全部九年数据。比较 `chunk-size` 1000/2000 与 `workers` 1/2，并确认样本编号、特征完整性和数值一致。
2. **格网微基准**：使用与 17MPU 参考格网对齐的固定 `1024 x 1024` 裁剪区域。测试 `(block-size, workers)` 组合 `(256,1)`、`(256,2)`、`(384,2)` 和 `(384,4)`。先测试标准端点；只有完成基线后，才对 `computePixels` 单独测试高吞吐端点。
3. **稀疏点与格网一致性**：分别通过点位路线和已完成 Zarr 提取固定候选像元中心。采用稀疏提取制作地图前，应计算完全相等比例、最大绝对差和余弦差异。
4. **完整瓦片单年测试**：用最快且稳定的配置下载 17MPU 的 2025 年数据，核验坐标系、仿射变换、尺寸、64 个波段、有效掩膜和磁盘大小。
5. **断点续传测试**：完成若干格网块后中断运行，再以相同命令重新执行。确认已完成数据块得到复用，并且最终抽样值与全新运行一致。
6. **完整 17MPU 九年测试**：仅在第 1–5 步通过后执行 2017–2025 年下载。应依据本次完整运行的实测时间和存储量决定是否批准 11 瓦片生产任务，而不是直接使用微基准线性外推。

正式生产配置必须优先保证完整性、稳定性与可靠续传，而不是追求短时测试中的最高吞吐量。任何导致间歇性缺块、配额失败或数值不一致的高速配置都应被拒绝。

## 批处理与 Google Drive 回退方案

只有当直接交互式请求反复超时，或组织流程明确要求批处理导出时，才使用本路线。

### 规划并提交年度点导出

首先必须检查试运行结果：

```bash
python scripts/export_aef_points_ee.py --sample-csv outputs/plantation_inventory/plantation_polygon_aef_points_all.csv --project YOUR_GEE_PROJECT --folder AEF_ALL_PLANTATION_POLYGONS --prefix plantation_polygon_pixels_2017_2025 --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --chunk-size 2000 --log-dir outputs/submitted_tasks --dry-run
```

确认后移除 `--dry-run` 提交任务。导出文件中每个点对应一行，每年包含 64 个特征列，例如 `aef2025_center_A00` 至 `aef2025_center_A63`。对于大任务，可以使用 `--start-chunk` 和 `--max-chunks` 分批控制提交范围。

### 下载已完成的 Drive 导出

```bash
python scripts/download_drive_exports.py --prefix plantation_polygon_pixels_2017_2025 --out outputs/raw_exports
```

下载器支持 `.part` 临时文件、重试和断点续传，并在最终提交文件前核验 Google Drive 的预期文件大小。

### 验证并合并分片

PowerShell 示例：

```powershell
python scripts/merge_aef_point_exports.py --master outputs/plantation_inventory/plantation_polygon_aef_points_all.csv --exports (Get-ChildItem outputs/raw_exports/plantation_polygon_pixels_2017_2025_part*.csv | ForEach-Object FullName) --out outputs/point_features/plantation_polygon_pixels_aef.parquet
```

合并程序会拒绝重复样本编号、年度 64 维特征不完整记录以及主表之外的导出编号，并在输出文件旁写入 JSON 质量检查报告。

## 聚合年度地块原型

```bash
python scripts/aggregate_polygon_aef.py --pixel-features outputs/point_features/plantation_polygon_pixels_aef.parquet --out-dir outputs/polygon_features --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --minimum-valid-fraction 1.0 --output-prefix plantation_polygon_aef
```

程序先对每个点进行 L2 归一化，再计算每个地块内部的加权平均方向，最后对平均向量再次归一化。完整产品为：

```text
outputs/polygon_features/plantation_polygon_aef_prototypes_complete.parquet
```

## 固定的全国背景样本流程

在已知人工林以外创建与模型得分无关、空间均衡的背景点池：

```bash
python scripts/build_global_background.py --lclu /path/to/lclu.gdb --lclu-layer SC_COBERTURA_TIERRA_A --plantations /path/to/plantations.shp --verified-negatives /path/to/verified_negatives.parquet --out-dir outputs/global_background_2025 --target-points 40000
```

随后使用 `export_aef_points_ee.py` 导出这些点，并将 `--years` 设置为 2025。下载各分片后执行验证：

```bash
python scripts/validate_background_exports.py --master outputs/global_background_2025/global_background_points_2025.csv --raw-dir outputs/global_background_2025/raw_exports --out-dir outputs/global_background_2025/features
```

## 年度栅格工作流

按照参考 GeoTIFF 的精确格网导出年度 64 波段栅格：

```bash
python scripts/export_aef_grid_ee.py --reference /path/to/reference.tif --project YOUR_GEE_PROJECT --prefix 17MPU_AEF --folder AEF_GRITS_RASTERS --task-log outputs/submitted_tasks/17MPU_AEF_tasks.json --dry-run
```

检查计划后移除 `--dry-run`。使用相同前缀下载文件：

```bash
python scripts/download_drive_exports.py --prefix 17MPU_AEF --out outputs/rasters/17MPU
```

为每一年建立一个 VRT，并根据参考文件验证全部格网：

```bash
python scripts/build_aef_raster_catalog.py --root outputs/rasters/17MPU --prefix 17MPU_AEF --tile-id 17MPU --reference /path/to/reference.tif --out outputs/rasters/17MPU/catalog.csv
```

## 验证

```bash
pytest
python -m compileall -q aef_grits samples scripts
```

测试覆盖 64 维数据结构、年份发现、单位超球面归一化、确定性空间块分配和直接流式读取。生产任务还会输出任务、合并、完整性和聚合报告，以支持数据级审计。

## 来源说明

生产流程与独立仓库文件之间的准确对应关系记录在 [`docs/source-map.md`](docs/source-map.md)。原始本地数据和输出产品没有复制到本仓库。

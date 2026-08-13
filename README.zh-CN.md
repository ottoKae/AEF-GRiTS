# AEF-GRiTS

[English](README.md) | [简体中文](README.zh-CN.md)

**支持平台：** `Windows` · `macOS（Intel / Apple Silicon）` · `Linux`<br>
**使用界面：** `Web网页应用` · `Python命令行`

AEF-GRiTS 是一套独立且可复现的年度 AlphaEarth Foundation（AEF）嵌入特征采样、导出、下载与验证工作流。它在运行和数据资源上均不依赖其他源码仓库，命令所需的全球MGRS格网表已经随本仓库和Python包发布。

本工作流支持四种下载方式：

1. **点位特征**：读取表格或矢量样本，统一转换为WGS84点位并写入Parquet分片。
2. **参考图格网**：按照任意10米参考 GeoTIFF 建立一个10米 Zarr。
3. **Tessera 0.1°格网**：每个 Tessera 风格小瓦片建立一个10米 UTM Zarr。
4. **MGRS格网**：按照包内全球MGRS表定义的格网，每个MGRS瓦片建立一个10米Zarr。

地块内部采样和单位超球面地块原型聚合建立在点位下载路线之上。

Earth Engine 数据源为 `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`，波段为 `A00` 至 `A63`。

## 仓库结构

```text
aef_grits/
├── features.py                     # AEF 数据结构、质量控制与球面运算
├── catalog.py                      # 通用栅格目录与格网辅助工具
├── earth_engine.py                 # Earth Engine 影像构建工具
├── atomic.py                       # 原子化 JSON 与 Parquet 写入
├── grids.py                        # 统一10米格网及三个格网提供器
├── points.py                       # 表格/矢量转换与下载前预检
├── point_store.py                  # 点位分片统一验证加载器
├── grid_lookup.py                  # AOI与MGRS/Tessera空间检索
├── resources.py                    # 包内资源解析器
├── data/mgrs.parquet               # 内置全球MGRS格网表
└── store.py                        # 本地 Zarr 与多瓦片目录读取器
samples/
├── aef_plantation_polygon_pixels.py
└── aef_balsa_polygon_pixels.py
scripts/
├── stream_aef_points_ee.py         # Earth Engine 点特征直传本地 Parquet 分片
├── stream_aef_grid_ee.py           # Earth Engine 格网直传本地 Zarr v3
├── prepare_aef_points.py           # 矢量/polygon转标准点位表
├── extract_aef_patches.py          # Zarr目录转分片NPY训练张量
├── merge_aef_point_shards.py       # 验证并合并点位分片
├── smoke_test_aef_tiles.py         # Tessera/MGRS有限范围在线测试
├── resolve_aef_grid_ids.py         # 矢量AOI转固定grid_id清单
├── search_aef_grid_catalog.py      # 中英文行政区/格网检索
├── visualize_aef_grid_lookup.py    # MGRS/Tessera快速覆盖图
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

### 单仓库保证

全新clone已经包含所有项目自有运行资源，包括19,002行全球MGRS格网表。普通命令不会扫描父目录，也没有指向本机其他源码仓库的硬编码路径。外部服务仅限按用户请求访问Earth Engine；用户提供的AOI、点表、参考栅格和可选catalog属于实验输入，不是源码仓库依赖。

clone或安装wheel后可以这样验证内置资源：

```bash
python -c "from aef_grits import mgrs_index_path; print(mgrs_index_path())"
python scripts/stream_aef_grid_ee.py --help
```

## Windows、macOS和Linux安装

Windows、Linux、Intel Mac和Apple Silicon Mac统一推荐使用独立的conda-forge环境。Windows请在Anaconda Prompt或PowerShell执行，macOS/Linux请在终端执行：

```bash
conda env create -f environment-download.yml
conda activate aef_grits_download
earthengine authenticate --auth_mode=localhost
earthengine set_project YOUR_GEE_PROJECT
aef-grits-doctor --project YOUR_GEE_PROJECT --output ./outputs
```

该环境同时包含Python下载命令和本地Web应用。安装后可直接使用`aef-grits-points`、`aef-grits-grid`和`aef-grits-doctor`。Linux本地/远程服务器、Intel/Apple Silicon Mac、Earth Engine登录、磁盘和长任务运行方法见[`docs/linux-macos-download.md`](docs/linux-macos-download.md)。

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

| 下载方式 | Earth Engine 接口 | 本地格式 | 适用场景 |
|---|---|---|---|
| 点位下载 | `computeFeatures` | 分片 Parquet | 训练点、地块中心和稀疏候选像元 |
| `reference` | `computePixels` | Zarr v3 | 局部实验和自定义10米参考区域 |
| `tessera_0p1` | `computePixels` | 每个0.1°单元一个 Zarr v3 | 小范围下载、快速试验和Tessera对比 |
| `mgrs` | `computePixels` | 每个MGRS瓦片一个 Zarr v3 | S1/AEF逐像元融合与正式制图 |

如果实验只需要数千个点，不要下载完整格网。只有后续分析需要反复访问大量任意像元或制作全覆盖地图时，才应使用 Zarr。

### 定义点位下载

#### 最小输入要求

| 输入 | 必需内容 | 坐标系要求 | 样本编号 |
|---|---|---|---|
| CSV | `sample_id,lon,lat` | `lon/lat`必须是WGS84经纬度（`EPSG:4326`） | `sample_id`必须唯一 |
| Shapefile | Point/MultiPoint或Polygon/MultiPolygon几何 | 必须有正确的`.prj`；AEF-GRiTS会根据声明的CRS自动转换到WGS84 | 用`--id-field`指定已有唯一字段；不指定时自动生成确定性编号 |

标签、树种、split、polygon编号和行政区等附加字段都会保留。Polygon默认每个要素生成一个保证位于内部的代表点；只有需要全部格网对齐内部像元时，才使用`--geometry-mode interior_pixels --reference-grid <GeoTIFF或Zarr>`。不要把投影坐标`x/y`直接放入CSV的`lon/lat`列，因为CSV本身不记录CRS；投影坐标表必须先转换成WGS84。

CSV示例：

```csv
sample_id,lon,lat,label,split
p0001,116.287048,39.533912,1,train
p0002,114.409640,35.472660,0,validation
```

```powershell
python scripts/stream_aef_points_ee.py --samples data/points.csv --out-dir outputs/points_2020 --project YOUR_GEE_PROJECT --years 2020
```

Shapefile示例：

```powershell
python scripts/stream_aef_points_ee.py --samples data/samples.shp --id-field id --out-dir outputs/points_2020 --project YOUR_GEE_PROJECT --years 2020
```

#### 为什么点位统一使用WGS84，而格网使用UTM分区

AEF的64维特征向量本身没有坐标系；坐标系属于定位这些向量的影像格网。Earth Engine中的AEF源影像使用各自所在地区的UTM分区，并不存在一个覆盖全球的统一UTM投影。因此，一张跨地区点位表可能同时涉及多个UTM EPSG代码。

AEF-GRiTS把WGS84（`EPSG:4326`）作为点位交换坐标系，原因是：

- 一组经纬度可以唯一表达全球任意位置；
- 同一张表可以跨越多个UTM分区，不需要每行另外保存EPSG代码；
- CSV、GIS软件和Earth Engine可以统一交换WGS84点位；
- `sampleRegions`按10米尺度采样时，Earth Engine会把点转换到相应AEF影像的投影。

因此，点位Parquet保留WGS84的`lon/lat`；对应的64维AEF只是特征值，不是带投影的几何。具有正确CRS声明的矢量输入会在提交前自动转换到WGS84。程序不会猜测投影CSV的坐标系，因为CSV自身不能可靠保存CRS元数据。

完整格网的需求正好相反。用于连续制图的Zarr必须具有固定的米制像元大小、确定的像元原点和明确的行列仿射关系。经纬度单位是度，尤其经度一度对应的地面距离随纬度变化，不能形成全球一致的10米正方形像元。因此，每个完整格网采用其所在地区的UTM分区：

- 像元保持10米×10米且使用米制单位；
- `CRS + affine transform + width + height`能够唯一确定每个像元；
- 可与MGRS边界和S1-GRiTS像元直接对齐；
- window、patch和逐像元推理可直接使用整数row/column，不必反复重投影。

两种约定在resolver中衔接：

```text
点位表：WGS84 lon/lat
        ↓ Earth Engine或PyProj坐标转换
本地AEF/Zarr的UTM坐标
        ↓ 逆仿射变换
像元row/column → 64维AEF向量
```

使用WGS84点查询本地Zarr时，`AEFZarr`会自动完成转换。点位位于瓦片重叠边缘时，应显式指定`grid_id`，以固定所使用的UTM格网和像元原点。如果要求点位结果与Zarr逐位一致，应从目标Zarr的像元中心生成WGS84坐标，不要使用靠近像元边界的任意坐标。

点位输入可以是CSV、Parquet、Shapefile、GeoPackage或GeoJSON。表格中每个待下载点占一行，并且必须包含：

```text
sample_id,lon,lat
```

- `sample_id` 必须唯一。
- `lon` 与 `lat` 必须是有限的 WGS84 坐标（`EPSG:4326`）。
- `polygon_id`、树种、瓦片、省份和样本权重等附加字段会保留在每个输出分片中。
- AEF 缺失或被掩膜的样本仍保留对应记录，其特征值为 NaN。
- 点位采样固定使用 AEF 原生10米尺度。程序会拒绝其他 `--scale`；30米等较粗对比产品应在下游单独生成。

Point和MultiPoint几何会直接保留。Polygon和MultiPolygon默认使用一个保证位于地块内部的代表点。`--geometry-mode`还支持：

- `representative`：每个polygon一个内部代表点，适合地块清单；
- `centroid`：每个polygon一个质心，但凹多边形的质心可能位于外部；
- `interior_pixels`：生成polygon内部的全部像元中心，必须同时用`--reference-grid`指定参考GeoTIFF或AEF Zarr，以保证像元严格对齐；`--max-points`用于防止意外生成过多点。

GeoPackage可通过`--layer`指定图层，通过`--id-field polygon_id`指定稳定的样本编号来源。已有候选像元CSV或Parquet可以直接输入。

例如，可以从已有点位清单中固定随机选择10,000个点，用于下载速度基准测试：

```python
import pandas as pd

source = pd.read_parquet("data/all_candidate_points.parquet")  # 也可以读取CSV
points = source.sample(n=10_000, random_state=20260811)
points[["sample_id", "lon", "lat"]].to_parquet(
    "data/aef_benchmark_10000.parquet", index=False
)
```

随机选择适合性能测试；正式模型训练仍应采用空间分层样本方案。下载器不会自行选择10,000个点，只会严格提取输入表中的记录，因此选取后必须保证 `sample_id` 仍然唯一。

如果只想先转换矢量而不下载AEF：

```powershell
python scripts/prepare_aef_points.py --input data/plantations.gpkg --layer plantations --id-field polygon_id --geometry-mode representative --output outputs/prepared_points/plantations.parquet
```

若要按照现有Zarr严格生成地块内部像元中心：

```powershell
python scripts/prepare_aef_points.py --input data/plantations.shp --id-field polygon_id --geometry-mode interior_pixels --reference-grid outputs/mgrs/17MPU/aef_17MPU_2017_2025.zarr --output outputs/prepared_points/plantation_pixels.parquet
```

对于人工林清查数据，可先在地块内部创建确定性的采样点：

```bash
python samples/aef_plantation_polygon_pixels.py --shapefile /path/to/plantations.shp --out-dir outputs/plantation_inventory
```

可以通过`--grid-catalog /path/to/catalog.parquet`附加任意兼容本地栅格目录中包含该点的格网信息；该参数读取的是用户数据，并不依赖其他代码仓库。主要点表为：

```text
outputs/plantation_inventory/plantation_polygon_aef_points_all.csv
```

### 直接提取点特征

`--out-dir`用于明确指定存储位置。省略时固定写入`<仓库>/outputs/point_stream`，不受当前PowerShell工作目录影响；也可以传入其他硬盘上的绝对路径。

正式下载前可先运行预检，不需要初始化Earth Engine，也不会发起数据请求：

```powershell
python scripts/stream_aef_points_ee.py --samples data/aef_points.parquet --out-dir D:/AEF/points_2017_2025 --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --validate-only
```

`validation_report.json`会检查空表、必需字段、经纬度有限性和范围、重复ID、重复坐标组、年份范围、特征数、预计分片数，以及已有split、树种和瓦片字段的数量分布。重复坐标作为警告报告；重复ID和无效坐标会阻止下载。

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

每个 Parquet 分片保留输入表中的全部元数据列，追加所选年份的 AEF 特征列、每年的完整性标记和 `aef_complete_all_years`。`catalog.parquet` 每行描述一个分片，`report.json` 汇总完整与不完整记录数。点位下载不写入 Zarr。

统一加载并验证全部分片：

```python
from aef_grits import load_aef_points

dataset = load_aef_points("outputs/point_stream", years=[2024, 2025])
points = dataset.frame
print(dataset.report)
```

加载器会拒绝缺失文件、混合运行签名、分片行数不一致、重复sample_id、缺失年份和年度64维字段不完整。设置`require_complete=True`可拒绝任何含NaN的样本。也可合并成一个经过验证的Parquet：

```powershell
python scripts/merge_aef_point_shards.py --catalog outputs/point_stream/catalog.parquet --output outputs/point_features/aef_points.parquet --require-complete
```

点采样属于 Earth Engine 聚合计算，建议初始使用一个工作线程。只有代表性计时测试稳定后，才增加至两个工作线程。

`--chunk-size` 控制本地断点续传单元，以及单个 Earth Engine 表达式中包含的坐标数量。`--page-size` 控制服务器响应的内部分页大小，不改变最终 Parquet 的分片方式。九年数据的每一行包含 576 个特征列：

```text
aef2017_center_A00 ... aef2017_center_A63
...
aef2025_center_A00 ... aef2025_center_A63
```

### 定义并下载10米格网

坐标系约定可以简要归纳为：

| 对象 | 坐标系/存储约定 |
|---|---|
| AEF向量 | 64个特征值本身没有坐标系 |
| 点位请求与结果 | WGS84经纬度（`EPSG:4326`） |
| 连续Zarr | 所在格网的本地UTM投影，10米正方形像元 |
| MGRS/Tessera编号 | 仅用于发现数据；精确CRS和仿射变换以Zarr元数据为准 |

Earth Engine会在内部把WGS84点位转换到源影像投影。连续格网使用UTM，
是因为经纬度的“度”不能定义处处相同的10米像元。

格网下载需要一个参考 GeoTIFF，其元数据构成完整的目标格网约定：

```text
坐标参考系 + 仿射变换 + 宽度 + 高度
```

`reference` 模式要求参考文件使用投影坐标系、北向格网和准确的10米像元。仅提供外接矩形不足以锁定像元原点。程序会拒绝非10米参考图；如果需要30米对比，应在下载10米原始产品后，另行生成并记录降尺度产品。

实现使用 Earth Engine 默认的最近邻重投影，以保留 AEF 向量的各个分量，不对嵌入维度进行插值。

```bash
python scripts/stream_aef_grid_ee.py --grid-scheme reference --reference /path/to/10m_reference.tif --out outputs/reference/aef_custom_2017_2025.zarr --catalog outputs/reference/catalog.parquet --tile-id custom_area --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --block-size 256 --workers 2
```

Tessera 风格格网使用中心位于 `0.05°` 偏移序列上的0.1°单元，例如 `grid_-79.95_-1.05`。0.1°单元负责数据发现和编号，内部像元保存为对齐到10米格点的本地 UTM 格网。可以指定一个或重复指定多个中心：

```bash
python scripts/stream_aef_grid_ee.py --grid-scheme tessera_0p1 --tessera-tile -79.95 -1.05 --out outputs/tessera_0p1/grid_-79.95_-1.05/aef_2017_2025.zarr --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025
```

也可以给出 WGS84 外接矩形，自动枚举所有相交的0.1°单元：

```bash
python scripts/stream_aef_grid_ee.py --grid-scheme tessera_0p1 --bbox -80.0 -1.2 -79.7 -0.9 --out-dir outputs/tessera_0p1 --project YOUR_GEE_PROJECT --years 2025
```

MGRS提供器直接读取随仓库发布的`aef_grits/data/mgrs.parquet`，使用其中的`utm_epsg`和`utm_wkt`，并将投影边界对齐到10米格点，不根据瓦片名称推测空间范围：

```bash
python scripts/stream_aef_grid_ee.py --grid-scheme mgrs --tiles 17MNT 17MNV 17MPT 17MPU 17MPV 17NQA --out-dir outputs/mgrs --catalog outputs/mgrs/catalog.parquet --project YOUR_GEE_PROJECT --years 2017 2018 2019 2020 2021 2022 2023 2024 2025
```

普通用户不需要提供外部MGRS文件。`--mgrs-index`和环境变量
`AEF_GRITS_MGRS_INDEX`只用于专家显式覆盖内置资源。

#### 将用户AOI转换为grid_id

`resolve_aef_grid_ids.py`接受Shapefile、GeoPackage、GeoJSON或GeoParquet，
根据输入文件声明的CRS自动转换，并同时生成MGRS和Tessera 0.1°编号。
对于polygon，默认只保留与AOI具有正面积交集的格网，单纯接触边界的格网不会误选；
对于点位文件，则只返回实际包含这些点的格网。Tessera单元互不重叠，因此一个点的
归属是确定的；包内MGRS覆盖在UTM分区和瓦片边缘有意保留重叠，一个点可能
合理地对应多个MGRS Zarr。完整区域下载应保留全部相交格网；单点提取则应明确选择目标格网。

```powershell
python scripts/resolve_aef_grid_ids.py `
  --aoi data/provinces.gpkg --layer provinces `
  --region-id-field province_code `
  --name-field-cn province_cn --name-field-en province_en `
  --out-dir outputs/grid_lookup/provinces
```

输出包括`mgrs_grid_ids.txt`、`tessera_0p1_grid_ids.txt`、逐行政区交叉表
`grid_intersections.parquet`、`grid_ids.json`和双语可检索的
`grid_catalog.parquet`。中文省名、英文省名和行政代码来自用户明确指定的边界字段，
程序不会依据MGRS编号猜测省名；因此空间相交关系始终是权威依据，同时支持智能体按中英文检索：

```powershell
python scripts/search_aef_grid_catalog.py `
  --catalog outputs/grid_lookup/provinces/grid_catalog.parquet `
  --query "圣多明各" --scheme mgrs --ids-only

$tiles = Get-Content outputs/grid_lookup/provinces/mgrs_grid_ids.txt
python scripts/stream_aef_grid_ee.py --grid-scheme mgrs `
  --tiles $tiles --out-dir outputs/mgrs --project YOUR_GEE_PROJECT --years 2025
```

在下载影像前，可以生成PNG、SVG和JSON核查报告：

```powershell
python scripts/visualize_aef_grid_lookup.py `
  --intersections outputs/grid_lookup/provinces/grid_intersections.parquet `
  --aoi data/provinces.gpkg --layer provinces `
  --out outputs/grid_lookup/provinces/grid_coverage.png `
  --title "Province AEF grid lookup"
```

推荐把智能体辅助下载明确拆成四步：

1. 告诉智能体AOI绝对路径、GeoPackage图层名（如有）、行政区编号字段、中英文名称字段、
   目标格网类型、年份、Earth Engine项目和输出目录。
2. 第一次只要求**解析格网并制图**，让智能体报告数量和编号，并明确说明暂不下载AEF。
3. 第二次只批准一个明确grid_id和一个年份的小规模下载，检查CRS、10米尺寸、完整率、
   耗时和磁盘占用。
4. 小测试通过后，再明确批准指定的grid_id清单和年份。省级任务执行前，应先让智能体
   展示准确命令与存储估算，不要仅凭省名直接启动全省下载。

第一轮对话可以直接写成：

```text
请在AEF-GRiTS中把以下AOI解析为MGRS和Tessera 0.1°格网：
AOI=D:/data/provinces.gpkg，layer=provinces，
行政区编号字段=province_code，中文名字段=province_cn，英文名字段=province_en。
输出到outputs/grid_lookup/provinces，并生成覆盖核查图。
请报告全部数量和grid_id，但暂时不要下载AEF。
```

确认后另行发出受限测试指令：

```text
使用刚生成的目录，只下载MGRS 50SNA的2025年AEF，
Earth Engine项目为YOUR_GEE_PROJECT。报告耗时、Zarr形状、CRS、
有效比例和磁盘大小，不要扩展到其他格网。
```

#### 安徽省真实案例

安徽省ADM1 polygon共选择28个MGRS格网（全部为EPSG:32650）和1,477个
Tessera 0.1°单元。中文`安徽省`、英文`Anhui Province`和代码`CN-AH`
检索得到完全相同的格网集合，目录中没有重复的`(scheme, grid_id)`记录。
本次只验证格网解析和可视化，没有启动安徽全省AEF下载。完整命令、MGRS清单和解释见
[`docs/anhui-grid-lookup-example.md`](docs/anhui-grid-lookup-example.md)。

多格网输出结构为：

```text
outputs/mgrs/
├── catalog.parquet
├── run_summary.json
├── 17MPU/
│   ├── aef_17MPU_2017_2025.zarr
│   ├── aef_17MPU_2017_2025.zarr.progress.json
│   └── aef_17MPU_2017_2025.zarr.report.json
└── ...
```

默认单次请求为 `256 x 256 x 64 x float32`，未压缩数据量约 16.8 MB，明显低于 Earth Engine `computePixels` 的 48 MB 限制。本地数据立方体为：

```text
embeddings(time, band, y, x) float32
chunks=(1, 64, 64, 64)
shards=(1, 64, 512, 512)
codec=Blosc(Zstd level 7, no shuffle, typesize 4)
```

64×64内部数据块是在点位、patch与连续窗口读取之间采用的固定折中；512×512的Zarr v3分片则能避免生成数百万个小文件。运行期间会持续提交进度账本，因此命令中断后可以使用相同参数继续执行。

该压缩配置完全无损，解码后的 float32 位模式与写入值一致。它被定义为第2版存储协议，并同时写入 Zarr 属性和 Parquet 目录。实测覆盖 17MNT、17MPU、17MPV、17NQA 四个空间分离瓦片的2017、2021、2025年：12个数据块比较和4个三年Zarr往返读取均逐位一致。新配置占原始 float32 字节数的17.9%—19.8%（中位数19.2%），较原来的 Zstd-3 + byte-shuffle 再减少63.1%—63.7%。这些是试验块实测结果，不是对每个完整瓦片的大小保证。

运行签名绑定数据源、年份、参考格网、请求分块、内部数据块和分片大小。如果使用不同约定复用同一输出路径，程序会拒绝执行，避免无提示地混合不同格网。

这里需要区分三个尺度：格网提供器决定最终数据范围；`--block-size` 只决定网络请求分块；Zarr 的 `chunks` 和 `shards` 只决定本地存储组织。例如，完整 MGRS 格网会生成完整 MGRS 瓦片，而一个对齐的 `1024 x 1024` 参考裁剪图只会生成同样大小的测试数据集。

### 读取本地 Zarr

```python
from aef_grits.store import AEFZarr, AEFCatalog

tile = AEFZarr("outputs/17MPU/aef/zarr/aef_17MPU_2017_2025.zarr")
values = tile.sample_points([(-79.30, -1.10)], years=[2024, 2025])
# values.shape == (1, 2, 64)

patches = tile.sample_patches(
    [(-79.30, -1.10)], patch_size=9, years=[2024, 2025]
)
# patches.shape == (1, 2, 64, 9, 9)

catalog = AEFCatalog("outputs/catalog.parquet")
values = catalog.sample_points([(-79.30, -1.10), (-80.10, 0.20)], years=[2025])

tile = catalog.open_tile("17MPU")
window = tile.read_window(0, 1024, 0, 1024, years=[2025])
# window.shape == (1, 64, 1024, 1024)

pieces = catalog.read_bbox(
    (-79.4, -1.2, -79.2, -1.0), years=[2025], crs="EPSG:4326"
)
# {grid_id: (values, affine_transform, crs), ...}
```

`AEFZarr.sample_points()` 返回 `[N,T,64]`，按照涉及的 `64 x 64` 数据块对点分组，每个数据块只读取一次。超出数据范围的点返回 NaN。`sample_patches()` 返回以给定像元为中心的奇数边长 patch，形状为 `[N,T,64,P,P]`，超出瓦片边界的部分填充 NaN；制作训练和验证张量时应分批读取。`AEFCatalog` 可以把点和patch路由到多个瓦片数据集；若点位于瓦片重叠边缘，应显式传入瓦片编号。

`open_tile()` 统一接受参考图编号、Tessera格网名或MGRS瓦片号；`read_window()` 可以在不加载完整数据立方体的情况下读取像素窗口；`read_bbox()` 为每个相交数据集返回保持原生格网的数据块，不会无提示地重投影或拼接不同 UTM 分区。对于区域级惰性处理，可以使用：

```python
ds = tile.open_xarray()
print(ds.embeddings.dims)  # ('time', 'band', 'y', 'x')
```

若要形成可直接供PyTorch读取的固定patch交付，可批量生成NPY张量分片和一一对应的Parquet元数据：

```powershell
python scripts/extract_aef_patches.py --samples data/model_points.parquet --catalog outputs/mgrs/catalog.parquet --out-dir outputs/patches_9x9 --years 2017 2018 2019 2020 2021 2022 2023 2024 2025 --patch-size 9 --batch-size 256 --grid-id-column mgrs_tile
```

每个NPY为`[N,T,64,P,P]`的float32，可用`numpy.load(path, mmap_mode="r")`打开，再交给`torch.from_numpy`。对应metadata分片保留标签、split、polygon_id、`patch_index`、完整性和有效比例。加入`--validate-only`时只报告张量形状和未压缩存储估算，不实际读取patch。

### 性能建议

当前机器和 Earth Engine 项目的实测结果为：

- 1,000 个真实点 × 9 年 × 64 维：16.1 秒，全部记录完整。
- 一个 `256 x 256 x 64` 格网块：27.3 秒，压缩后约 10.9 MB。
- 重新运行时，已完成的点分片和格网块可直接从进度记录中复用，无需再次下载。

当前点采样性能足以支持现有样本规模的实验。完整 11 瓦片格网仍需调优。正式生产前，应在单个瓦片上比较 `--block-size` 256 与 384，以及 `--workers` 1、2 和 4。只有在不增加 HTTP 429、超时或聚合错误的情况下提高吞吐量，才保留对应配置。默认不要对点聚合开启高吞吐端点；它主要适合大量简单像素请求。

最大的性能收益来自空间选择，而不是提高并发。如果第二阶段只对第一阶段候选像元进行分类，应将候选像元中心转换为点表，并使用点特征提取器。只有全覆盖可视化、反复任意查询或未来长期复用足以抵消额外传输和存储成本时，才下载完整瓦片数据立方体。

例如，如果第一阶段候选区只占全部瓦片像元的 5%，稀疏点下载只需请求并保存约 5% 的嵌入值，因此理论上可减少约 95% 的 AEF 传输量和特征存储量。由于 Parquet 元数据、请求初始化和数据目录具有固定开销，实际降幅会略低。只有项目需要完整 AEF 空间图、反复查询任意像元、开展全覆盖诊断或在当前候选掩膜之外长期复用数据时，才应下载完整 Zarr。

完整MGRS瓦片Zarr用于逐像元推理；点位Parquet用于pixel或中心点训练与空间独立验证；3×3、5×5、9×9训练输入通过 `sample_patches()` 从权威Zarr按需读取，默认不再复制一套完整patch数据。

包内`utm_wkt`中的17MPU边界在10米分辨率下形成`10980 x 10980`格网。单年未压缩数据约为30.86 GB，九年约为277.77 GB。按本次多瓦片试验的17.9%—19.8%压缩率外推，这个特定九年瓦片约为50—55 GB，但最终大小仍必须由完整下载确认。因此，全国11瓦片稠密下载仍需要单独做出存储决策；如果训练只需要少量候选像元，应优先采用点位Parquet或从Zarr按需读取patch。

### Chunk大小实测

`scripts/benchmark_zarr_chunks.py`可对真实数据的逐位一致副本测试resolver。四瓦片、三年份热缓存测试中，64×64将256×256整块读取中位耗时从86.4毫秒降低到62.3毫秒；但单点从3.21毫秒增加到9.46毫秒，9×9 patch从3.80毫秒增加到6.62毫秒，存储量也增加1.9%。因此64×64适合完整瓦片推理，不是所有访问模式下都更优。如果完整Zarr采用64×64，应继续用Parquet提供点位训练数据，并将有限训练patch一次性提取为分片张量。

### 运行 11 瓦片格网前必须完成的基准测试

所有测试均应使用固定的 17MPU 参考格网、相同 Earth Engine 项目和相同本地磁盘，并记录总耗时、成功数据块每秒、重试次数、HTTP 429/超时、有效像元数和最终磁盘占用。

1. **点下载基线**：对固定的 10,000 个点下载全部九年数据。比较 `chunk-size` 1000/2000 与 `workers` 1/2，并确认样本编号、特征完整性和数值一致。
2. **格网微基准**：使用与 17MPU 参考格网对齐的固定 `1024 x 1024` 裁剪区域。测试 `(block-size, workers)` 组合 `(256,1)`、`(256,2)`、`(384,2)` 和 `(384,4)`。先测试标准端点；只有完成基线后，才对 `computePixels` 单独测试高吞吐端点。
3. **稀疏点与格网一致性**：分别通过点位路线和已完成 Zarr 提取固定候选像元中心。采用稀疏提取制作地图前，应计算完全相等比例、最大绝对差和余弦差异。
4. **完整瓦片单年测试**：用最快且稳定的配置下载10米 17MPU 的 2025 年数据，核验坐标系、仿射变换、尺寸、64 个波段、有效掩膜和磁盘大小。
5. **断点续传测试**：完成若干格网块后中断运行，再以相同命令重新执行。确认已完成数据块得到复用，并且最终抽样值与全新运行一致。
6. **完整 17MPU 九年测试**：仅在第 1–5 步通过后执行 2017–2025 年下载。应依据本次完整运行的实测时间和存储量决定是否批准 11 瓦片生产任务，而不是直接使用微基准线性外推。

下载完整瓦片之前，先同时验证一个真实Tessera 0.1°格网和一个真实MGRS格网：

```powershell
python scripts/smoke_test_aef_tiles.py --project YOUR_GEE_PROJECT --out-dir outputs/tile_smoke --year 2025 --probe-size 256 --tessera-tile -79.95 -1.05 --mgrs-tile 17MPU
```

该命令使用真实的Tessera和MGRS格网定义，但各自只下载中心`256×256`数据块，用于验证CRS、仿射变换、格网编号、64维完整性、catalog登记和默认无损压缩，不会把一次冒烟测试误变成完整MGRS瓦片下载。

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

## 本地网页任务规划器

AEF-GRiTS 在Windows、macOS和Linux上均可运行只监听本机的网页界面。主界面简化为“选择点位或格网 → 选择输入与空间范围 → 选择年份和存储位置 → 下载”。点位支持带`sample_id,lon,lat`的WGS84 CSV，以及只含Point/MultiPoint且包含`.prj`的完整Shapefile；Polygon会在预检时拒绝。格网只提供MGRS和Tessera 0.1°，空间范围可以进行矢量绘制或使用带正确`.prj`的Shapefile。存储位置通过受输出根目录约束的服务端目录选择器确定。全部工作流仍采用带签名的“预检—确认—执行”两阶段协议，并保留有界队列、进程树取消、数据量与磁盘硬限制、结构化进度、自动产物验证、权威CRS格网覆盖和科研复现元数据。

```bash
conda activate aef_grits_download
python webapp/app.py
```

随后访问`http://127.0.0.1:5555`，依次选择下载目标、输入/空间范围、年份和存储子目录，点击“开始下载”；系统会先预检并要求确认。具体CRS契约、精确确认语义、产物验证、浏览器端到端测试、输出根目录规则和运行配置见[webapp/README.md](webapp/README.md)。

## 验证

```bash
pytest
python -m compileall -q aef_grits samples scripts
```

测试覆盖 64 维数据结构、年份发现、单位超球面归一化、确定性空间块分配和直接流式读取。生产任务还会输出任务、合并、完整性和聚合报告，以支持数据级审计。

## 来源说明

生产流程与独立仓库文件之间的准确对应关系记录在 [`docs/source-map.md`](docs/source-map.md)。原始本地数据和输出产品没有复制到本仓库。

# AEF-GRiTS

[English](README.md) | [简体中文](README.zh-CN.md)

AEF-GRiTS用于从Google Earth Engine直接下载年度64维AlphaEarth Foundation
嵌入特征，并保存到本地。它同时支持稀疏点位样本和连续10米格网，不需要通过
Google Drive中转。

**平台：** Windows、macOS（Intel/Apple Silicon）、Linux

**界面：** 命令行、本地Web应用、共享OAuth Web部署

数据集：`GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`；波段`A00`–`A63`；
年份2017–2025。

## 主要功能

- 支持CSV、Parquet、Shapefile、GeoPackage和GeoJSON点位输入。
- 支持Tessera 0.1°、MGRS和参考栅格三种连续格网。
- 10米float32 Zarr v3，采用无损Zstd-7/no-shuffle压缩。
- 原生UTM像元采样，保存源资产版本与格网；支持年度GeoTIFF交付。
- 有界内存与并发、请求deadline、错误分类、断点续传、原子元数据、磁盘保护和
  自动产物验证。
- 提供点位、patch和窗口读取器，可直接用于训练和推理。
- 仓库内置权威MGRS几何，不依赖其他项目。
- 自动发现已有凭据，并支持共享Web的每用户OAuth。

## 安装

```bash
conda env create -f environment-download.yml
conda activate aef_grits_download
```

也可以安装到已有Python 3.10+环境：

```bash
python -m pip install -e ".[download,web]"
```

## 主动完成认证

下载、重试和断点续传绝不自动打开浏览器。用户先通过独立命令登录，再验证账号
和Project：

```bash
aef-grits-auth login \
  --source earthengine \
  --auth-mode localhost \
  --project YOUR_GEE_PROJECT

aef-grits-auth verify --source auto --project YOUR_GEE_PROJECT --check-aef
aef-grits-doctor --auth-source auto --project YOUR_GEE_PROJECT --output ./outputs
```

`auto`优先复用显式ADC配置，否则使用当前用户Earth Engine凭据，最后检查本地或
云环境ADC。显式来源无效时直接失败，不会切换成其他用户。Project可通过
`--project`或用户私有配置提供；仓库中没有真实Project默认值。

个人电脑、SSH服务器、校园共享Web、服务账号、Google Cloud和CI的完整策略见
[认证与身份边界](docs/authentication.zh-CN.md)。

## 点位下载

CSV必须包含唯一`sample_id`以及WGS84的`lon`和`lat`。矢量输入必须声明CRS，
程序在Earth Engine采样前自动转换到WGS84。

```bash
# 仅检查输入，不访问Earth Engine
aef-grits-points --samples samples.csv --years 2024 2025 --validate-only

# 使用已经配置好的凭据下载
aef-grits-points \
  --samples samples.csv \
  --project YOUR_GEE_PROJECT \
  --years 2024 2025 \
  --out-dir outputs/points
```

结果为原子Parquet分片，并包含`catalog.parquet`和验证/报告JSON。大型输入采用
流式预检和有界批量下载，不会整表载入内存。

### 原生像元对齐

新下载采用 `native-utm-v1`。WGS84 的 `lon,lat` 用来定位，特征取自该点所在的
**原生 UTM 10米像元**。每年记录源资产、CRS、像元行列号和中心坐标；分片旁的
`.native.json` 保存源版本和格网。点位优先使用当地UTM区；重叠来源也只在自身
原生格网采样，并记录实际源CRS，不能默认它与任意指定UTM栅格相同。

格网只使用与目标CRS、像元边界一致的源，不跨UTM填补，不接受非整数像元错位，
缺测保留NaN。相反行序可无损重排；MGRS编号或0.1°选区本身不证明原生一致，
与其他传感器融合前仍需比较具体格网。

离线规划不等于已核验远端来源。正式下载先核验，并保存来源清单。
**旧成果和检查点不能直接接续新协议，请使用新的输出目录。**已完成TIFF-only
恢复只核对冻结的清单，不声称再次验证远端版本。旧Drive点位导出仅保留规划功能，
请使用上面的直接下载命令。这里保证像元身份与float32取值，不保证源double编码逐位相同。

详见[本次发布范围](docs/native-utm-release-20261102.md)及
[真实数据验收报告](docs/native-alignment-implementation-20261101.zh-CN.md)。

## 连续格网下载

```bash
# 只规划，不访问Earth Engine
aef-grits-grid \
  --grid-scheme mgrs \
  --tiles 17MPU \
  --years 2025 \
  --out-dir outputs/mgrs \
  --plan-only

# 去掉--plan-only，并提供或配置Project后下载
aef-grits-grid \
  --grid-scheme tessera_0p1 \
  --tessera-tile -79.95 -1.05 \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --out-dir outputs/tessera
```

每个grid ID对应一个Zarr，数据形状为`[year,64,y,x]`；chunks为
`(1,64,64,64)`；shards为`(1,64,512,512)`；float32、Zstd level 7、
no shuffle。

使用 `--export-geotiff` 保留Zarr和年度64波段GeoTIFF，或 `--geotiff-only` 仅交付
TIFF；两个参数互斥。TIFF保留float32、CRS、transform、波段名和NaN，使用无损DEFLATE，
需要时采用BigTIFF。仅TIFF模式通过本任务专属临时Zarr下载，TIFF校验成功后才清理缓存；
失败保留缓存，已有Zarr不会删除。峰值磁盘需同时容纳缓存和TIFF；两种TIFF模式均拒绝
Linux NTFS目标。

Linux NTFS/NTFS3只能接收已完成产物，活动状态和暂存必须位于ext4/XFS：

```bash
aef-grits-grid \
  --grid-scheme mgrs --tiles 50RMU --years 2019 \
  --project YOUR_GEE_PROJECT \
  --out-dir /mnt/hdda/user/aef \
  --state-dir /home/user/aef_state \
  --staging-dir /home/user/aef_staging
```

详见[NTFS安全运行](docs/ntfs-safe-download.zh-CN.md)和
[资源有界下载](docs/resource-bounded-download.md)。

## Web应用

单个可信本地用户直接运行：

```bash
python webapp/app.py
```

访问`http://127.0.0.1:5555`，填写Earth Engine Project，选择点位或格网、年份和
输出位置，按“预检—确认—执行”启动任务。Web端调用同一套命令行下载器。

校园共享服务器应启用每用户OAuth，不设置全局Project回退。每位用户登录自己的
Google/Earth Engine账号，并选择自己有权使用的Project；任务、凭据和输出按匿名
用户ID隔离。部署方式见[认证说明](docs/authentication.zh-CN.md)和
[Web应用说明](webapp/README.md)。

## 读取结果

```python
from aef_grits import open_aef_point_dataset
from aef_grits.store import AEFZarr

points = open_aef_point_dataset("outputs/points", years=[2025])
for batch in points.iter_batches(years=[2025]):
    consume(batch)

cube = AEFZarr("outputs/mgrs/17MPU/aef_17MPU_2025_2025.zarr")
vector = cube.sample_at(x, y, year=2025, crs="EPSG:32717")
patches = cube.sample_patches([(x, y)], years=[2025], patch_size=9, crs="EPSG:32717")
window = cube.read_window(0, 256, 0, 256, years=[2025])
```

标准Zarr接口会读取压缩元数据并自动解压。

仅TIFF成果请用QGIS或Rasterio读取；其catalog不是Zarr目录，不能传给
`AEFZarr`或`AEFResolver`。

## 用智能体调度下载

仓库随附[下载skill](.agents/skills/aef-grits-download/SKILL.md)，指导本机或SSH任务
规划、已有数据检查、有界执行和交付验收。向智能体说明执行位置、年份、点位/格网和
输出目录；远端还需SSH别名、解释器与资源预算。skill随源码分发，不由Python wheel
安装，也不会扩大授权范围、自动登录、删除旧数据或启动更大的下载。

## 根据AOI检索格网

```bash
python scripts/resolve_aef_grid_ids.py \
  --aoi province.shp \
  --schemes mgrs tessera_0p1 \
  --out-dir outputs/grid_lookup
```

## 测试

```bash
python -m pytest -q
```

CI在Windows、macOS和Linux上测试Python 3.11/3.12；浏览器流程使用Playwright。
自动测试只使用合成数据，不发起大规模Earth Engine下载。

## 仓库结构

```text
aef_grits/       认证、格网、读取器、存储和资源控制
scripts/         点位/格网下载器与准备工具
webapp/          本地/共享Web界面和可靠任务运行器
tests/           单元、API、CRS、存储和浏览器测试
docs/            数据组织、运行和部署说明
```

其他资料：[数据组织](docs/data-layout.md)、[直接流式下载](docs/direct-streaming.md)、
[Linux/macOS环境](docs/linux-macos-download.md)。

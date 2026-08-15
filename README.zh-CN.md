# AEF-GRiTS

[English](README.md) | [简体中文](README.zh-CN.md)

**支持平台：** Windows · macOS（Intel和Apple Silicon）· Linux<br>
**使用方式：** 本地Web应用 · Python命令行

AEF-GRiTS用于从Google Earth Engine直接下载年度AlphaEarth Foundation
（AEF）64维嵌入特征并保存到本地。它同时支持稀疏训练样本和连续10米制图格网，
不需要通过Google Drive中转。

数据源：`GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`，波段为`A00`—`A63`。

## 主要功能

- 从CSV、Shapefile、GeoPackage、GeoJSON或Parquet提取点位特征。
- 按Tessera 0.1°、MGRS或参考栅格下载10米Zarr数据立方体。
- 使用本地Web界面选择输入、年份、空间范围和存储目录。
- 支持断点续传、原子元数据、磁盘/数据量检查和产物验证。
- 提供点位、窗口、bbox及模型patch读取器。
- 仓库内置全球MGRS格网，不依赖其他源码仓库。

## 快速安装

安装Miniforge或Conda，克隆仓库后执行：

```bash
conda env create -f environment-download.yml
conda activate aef_grits_download
earthengine authenticate --auth_mode=localhost
earthengine set_project YOUR_GEE_PROJECT
aef-grits-doctor --project YOUR_GEE_PROJECT --output ./outputs
```

同一个环境文件适用于Windows、macOS和Linux。Windows请使用Anaconda Prompt
或PowerShell；远程Linux和macOS说明见[Linux与macOS配置](docs/linux-macos-download.md)。

## Web应用

在仓库根目录启动：

```bash
conda activate aef_grits_download
python webapp/app.py
```

浏览器访问`http://127.0.0.1:5555`，然后：

1. 选择“点位”或“格网”。
2. 选择点位文件；或者选择MGRS/Tessera，再通过Shapefile或矢量绘制设置AOI。
3. 选择年份和服务端存储子目录，点击“开始下载”。

服务端会先完成预检，再要求用户确认。底部任务栏显示进度、当前格网ETA、
输出路径、产物验证、日志和科研复现信息。

默认输出根目录为`webapp/output`。修改方法：

```bash
# Linux/macOS
export AEF_GRITS_WEB_OUTPUT=/data/aef
python webapp/app.py

# Windows PowerShell
$env:AEF_GRITS_WEB_OUTPUT = "D:\data\aef"
python webapp/app.py
```

Web应用只监听localhost，定位为可信本机单用户工具。详细说明见
[webapp/README.md](webapp/README.md)。

## 点位下载

CSV必须包含唯一的`sample_id`、`lon`和`lat`字段，坐标系为WGS84。矢量文件
必须声明CRS，程序会在Earth Engine采样前统一转换为WGS84。

只做输入检查，不访问Earth Engine：

```bash
aef-grits-points \
  --samples samples.csv \
  --years 2024 2025 \
  --validate-only
```

下载年度特征并保存为原子Parquet分片：

```bash
aef-grits-points \
  --samples samples.csv \
  --project YOUR_GEE_PROJECT \
  --years 2024 2025 \
  --out-dir outputs/points
```

大型CSV、Parquet和矢量输入采用有界分块完成预检和下载。程序对点位内容生成
稳定签名，并使用原生文件系统上的SQLite精确检查跨分块重复ID，不把整张表
载入内存。

大型点位结果可以逐批读取，无需拼接全部Parquet分片：

```python
from aef_grits import open_aef_point_dataset

points = open_aef_point_dataset("outputs/points", years=[2025])
for batch in points.iter_batches(columns=["sample_id"], years=[2025]):
    consume(batch)
```

Shapefile、GeoPackage、GeoJSON以及polygon转点使用相同命令，并通过
`--geometry-mode`选择转换方式。完整参数见`aef-grits-points --help`。

## 连续格网下载

所有连续产品均采用10米本地UTM格网和以下无损存储协议：

- Zarr v3、float32
- chunks：`(1,64,64,64)`
- shards：`(1,64,512,512)`
- Zstd level 7、no shuffle

先规划一个Tessera 0.1°瓦片，不执行下载：

```bash
aef-grits-grid \
  --grid-scheme tessera_0p1 \
  --tessera-tile -79.95 -1.05 \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --out-dir outputs/tessera \
  --plan-only
```

确认后去掉`--plan-only`即可下载。MGRS示例：

```bash
aef-grits-grid \
  --grid-scheme mgrs \
  --tiles 17MPU \
  --project YOUR_GEE_PROJECT \
  --years 2025 \
  --out-dir outputs/mgrs \
  --plan-only
```

任意10米GeoTIFF也可以作为`reference`格网。所有格网模式和性能参数见
`aef-grits-grid --help`。

## 根据AOI检索格网ID

将Shapefile、GeoPackage、GeoJSON或GeoParquet AOI转换为固定的MGRS和
Tessera格网清单：

```bash
python scripts/resolve_aef_grid_ids.py \
  --aoi province.shp \
  --schemes mgrs tessera_0p1 \
  --out-dir outputs/grid_lookup
```

`aef_grits/data/mgrs.parquet`是MGRS空间边界的权威来源。

## 输出结构

| 工作流 | 主要产物 | 辅助文件 |
|---|---|---|
| 点位 | Parquet分片 | `catalog.parquet`、验证/报告JSON |
| 连续格网 | 每个grid ID一个Zarr | `catalog.parquet`、进度/报告JSON |
| Web任务 | 与命令行相同 | 任务日志、事件、验证和复现信息 |

下载数据、认证信息、日志、任务状态和本地输出均不会进入Git。

## 内存受限服务器并发

点位和格网命令均支持 `--workers auto`。程序会综合主机可用内存、Linux
cgroup剩余额度和用户指定的 `--memory-limit-gib`，自动确定并发数。格网
请求采用有界在途队列，点位任务不再一次性创建全部Future。多个CLI或Web
任务共享Earth Engine请求令牌池，最终产物交付使用全局独占锁。

独立启动多个任务时，应指定同一个ext4/XFS资源状态目录：

```bash
export AEF_GRITS_RESOURCE_STATE=/home/user/aef_state/.resource_locks
```

计划和报告会保存最终采用的worker数、估计峰值内存、实际peak RSS和节流
次数。详细说明见[内存有界下载](docs/resource-bounded-download.md)。

每次Earth Engine请求都有明确deadline（默认300秒）、错误分类、有界退避和
可恢复失败事件。可用`--request-timeout-seconds`修改deadline。资源配置可选
`workstation-auto`、`low-memory-1g`、`server-8g`和`server-16g`。

## 读取本地Zarr

```python
from aef_grits.store import AEFZarr

cube = AEFZarr("outputs/mgrs/17MPU/aef_17MPU_2025_2025.zarr")
vector = cube.sample_at(x, y, year=2025, crs="EPSG:32717")
patches = cube.sample_patches(
    [(x, y)], years=[2025], patch_size=9, crs="EPSG:32717"
)
window = cube.read_window(0, 256, 0, 256, years=[2025])
```

resolver继续使用标准Zarr接口，压缩数据会自动解压。

## 可靠性设计

- 点位分片和元数据采用原子写入。
- 连续格网按块保存checkpoint，可恢复兼容的未完成输出。
- 请求签名防止不一致任务覆盖已有数据。
- Web应用提供一次性签名计划、有界队列、进程树管理、磁盘限制和自动验证。
- 每个格网记录CRS、transform、年份、波段、压缩和数据源信息。

## 仓库结构

```text
aef_grits/       格网、点位转换、读取器、内置资源和环境检查
scripts/         点位/格网下载器及数据准备工具
webapp/          本地Web界面和任务运行器
tests/           单元、API、存储、CRS和浏览器测试
docs/            数据组织与运行文档
```

详细文档：

- [数据组织](docs/data-layout.md)
- [Earth Engine直传本地](docs/direct-streaming.md)
- [Linux与macOS下载环境](docs/linux-macos-download.md)
- [Web应用](webapp/README.md)

## 测试

```bash
python -m pytest -q
```

如果系统存在Chrome/Chromium，测试会包含Playwright浏览器流程；自动测试不会启动
大规模Earth Engine下载。

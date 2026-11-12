# AEF-GRiTS

> **测试阶段：AEF 下载流程与 Web GUI 下载仍在测试中，尚非生产就绪版本。**
> 请先用少量点位或小范围任务试用，核对输出后再扩大下载规模。

[English](README.md) | [简体中文](README.zh-CN.md)

> **网页开发分支：** `feature/web-workbench` 保存尚未完成的首页和工作台优化，
> 暂不合入 `main`。它已接入正式分支的原生UTM下载内核，不代表网页已完成发布。

AEF-GRiTS用于从Google Earth Engine直接下载年度64维AlphaEarth Foundation
嵌入特征，并保存到本地。它同时支持稀疏点位样本和连续10米格网，不需要通过
Google Drive中转。

**平台：** Windows、macOS（Intel/Apple Silicon）、Linux

**界面：** 本地Python运行、浏览器操作（推荐）；保留命令行和可选共享OAuth部署。

数据集：`GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`；波段`A00`–`A63`；
年份2017–2025。

## 主要功能

- 支持CSV、Parquet、Shapefile、GeoPackage和GeoJSON点位输入。
- 支持Tessera 0.1°、MGRS和参考栅格三种连续格网。
- 10米float32 Zarr v3，采用无损Zstd-7/no-shuffle压缩。
- 格网可选Zarr、Zarr + 年度64波段GeoTIFF，或仅保留GeoTIFF。
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

也可在仓库或解压后的源码ZIP目录中安装；推荐使用现有CI覆盖的Python 3.11/3.12：

```bash
python -m pip install ".[download,web]"
aef-grits-web
```

启动器会打开`http://127.0.0.1:5555/portal`。命令不在PATH时，同一环境可执行
`python -m webapp.launcher`。这是本地源码安装方式，不代表已经发布PyPI版本或公开官网。

## 用智能体调度本地或 SSH 下载

仓库随附 [aef-grits-download skill](.agents/skills/aef-grits-download/SKILL.md)，
支持任务规划、已有数据检查、本地/SSH 下载、进度监控、续传和交付验收。
在本仓库目录启动支持 skills 的智能体；未自动发现时，让它先读取上述文件，
再按需求调用现有 Python 命令。无需另装其他 GRiTS 仓库或复制全局 skill。

例如对智能体说：

> 使用 $aef-grits-download，检查我的 samples.csv，规划 2025 年 AEF 点位下载。
> 输出到我指定的数据目录，先做离线预检，不登录、不下载。报告样本数、容量和缺失条件。

> 使用 $aef-grits-download，在我指定的 SSH alias、仓库和 Python 环境运行
> 50RMU 的 2025 年任务。先检查已有数据、内存和磁盘；按约定预算执行，保留断点。
> 完成后核验实际数据，给出服务器目录与报告；暂不回传本机。

提供执行位置、年份、点位/AOI/grid ID、输出格式和存储位置；远端还需指定 SSH
身份、解释器与资源预算。认证由执行机器上的当前用户完成，不把凭据交给官网。
智能体在已授权范围内调度，skill 不会扩大下载范围、自动登录、删除旧数据或保证
无限并发。**远端下载默认留在服务器，回传是另一个明确步骤。**

只有源码 checkout/包含该目录的源码包带有此 skill；Python wheel 不自动安装它。
`--plan-only` / `--validate-only` 不访问 GEE，但会写少量本地规划/状态文件。
详见 [命令与验收约定](.agents/skills/aef-grits-download/references/commands.md)和
[SSH 操作约定](.agents/skills/aef-grits-download/references/ssh.md)。

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
**原生 UTM 10 m 像元**，不再从经纬度拼接格网采样。每年记录源资产、CRS、像元
行列号及中心坐标；分片旁的 `.native.json` 保存资产版本和源格网。
点位优先使用当地UTM区；该区缺测时，重叠来源也只按其自身原生格网取值，实际CRS
会记录在结果中，不能默认它与某幅指定UTM栅格相同。

格网下载只使用与目标 CRS、像元边界一致的来源，不跨 UTM 填补，不接受错位的
参考格网；缺测保留 NaN。南向／北向行序的翻转不改变像元。MGRS 和 0.1° 是组织
单元，不代表必然与其他传感器逐像元对齐。

离线预检不等于已验证源网格；正式下载会先联网核验，Zarr、TIFF 和 catalog 均保存
来源信息。旧成果和检查点不能直接混入新协议，**请使用新的输出目录**。已完成 TIFF
可凭冻结的来源清单离线恢复，这不表示重新检查过远端源版本。旧 Drive 点位脚本仅
保留规划功能，下载请使用上面的直接下载命令。这里保证的是像元位置与 float32
取值一致，不是源 double 编码逐位无损。

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

常规GIS使用可选择**Zarr + GeoTIFF**（`--export-geotiff`）或**仅GeoTIFF**
（`--geotiff-only`），两个参数互斥。每年输出一个64波段float32 TIFF，采用无损DEFLATE，
保留CRS、transform、波段名和NaN NoData；未压缩量达到4 GiB时明确使用BigTIFF。

仅GeoTIFF模式先用本任务专属的临时Zarr缓存下载，TIFF校验成功后才清理该缓存；最终
保留TIFF、catalog/manifest和报告。失败时保留缓存供续传，**不会删除已有Zarr数据**。
下载期间的峰值磁盘仍需容纳缓存和TIFF两份数据；Zarr + GeoTIFF则长期保留两份。
导出按窗口执行，不整幅载入内存。当前Linux NTFS/NTFS3目标拒绝TIFF导出。点位仍输出Parquet。

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

完成安装后，单个可信本地用户运行：

```bash
aef-grits-web
# 可指定数据盘；--no-browser只打印地址、不自动打开浏览器
aef-grits-web --output-root /your/data/aef --no-browser
```

四步为：**P0连接 → P1点位/格网 → P2年份与查询 → P3本机存储与下载**。
P0主动检测凭据，必要时点击本机Google授权，再验证自己的Project与AEF访问。
页面按当前状态突出下一步；验证后收起设置，修改Project后须重新验证。
首次可下载工作台内的四点CSV示例，上传并选择2025年；完成后在下载管理中查看结果或复制路径。
“找到凭据”不代表验证通过。四项准备检查各占25%，不是实际下载进度；提交前仍须预检和确认。

新增网页工作台：`http://127.0.0.1:5555/portal`。支持产品选择、规划列表、计划导出、
地图定位和表格式下载管理；原简洁界面保留在`/`。目前只开放AEF，
Tessera 0.1°表示存储格网，不是Tessera特征产品。
当前GUI下载主路线是GEE；匿名源入口仅提供实验连通检查/读取库，**尚未贯通GUI下载**。

Python直接写入本机目录，不需要浏览器文件保存接口或打包ZIP传回。默认数据目录为
用户目录下`Downloads/AEF-GRiTS`，任务状态和日志位于用户应用状态目录，不写入安装包。
关闭网页标签不停止Python；退出进程、休眠等可能中断任务，重启后主动续传。

本机模式点击文件夹选择框会打开**系统文件夹窗口**，可选择其他磁盘；任务将在所选目录
内自动建立独立子目录，取消选择不改变原设置。预检检查所选磁盘，并把路径绑定到下载计划。
没有Tk或桌面会话时可使用独立的“网页目录浏览”入口，仍限制在配置的输出根目录内。
系统窗口需要Python环境具有可选Tk支持；Debian/Ubuntu系统Python可安装发行版的
`python3-tk`系统包，不能用`pip install tkinter`代替。远程部署时不能打开访问者电脑
的目录窗口：系统窗口属于运行Python的机器。

原`webapp/runs`和`webapp/output`不会自动搬迁。先停止旧实例，再用`--state-dir`和
`--output-root`显式指定原目录的绝对路径即可复用；安装更新不会自动重启正在运行的GUI。
见[网页工作台使用指南与截图](docs/web-portal.zh-CN.md)。

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

标准Zarr接口会读取压缩元数据并自动解压。仅GeoTIFF结果可直接使用QGIS或Rasterio读取，
不依赖已清理的临时Zarr缓存；其catalog不是Zarr索引，不能交给`AEFZarr`或`AEFResolver`。

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
.agents/skills/  随仓库分发的智能体下载操作规程
aef_grits/       认证、格网、读取器、存储和资源控制
scripts/         点位/格网下载器与准备工具
webapp/          本地/共享Web界面和可靠任务运行器
tests/           单元、API、CRS、存储和浏览器测试
docs/            数据组织、运行和部署说明
```

其他资料：[数据组织](docs/data-layout.md)、[直接流式下载](docs/direct-streaming.md)、
[Linux/macOS环境](docs/linux-macos-download.md)。

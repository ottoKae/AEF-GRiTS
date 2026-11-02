# AEF 源掩膜覆盖率检查

[check_aef_coverage.py](../scripts/check_aef_coverage.py) 是可独立复制运行的 Python 脚本，只依赖标准库和 `earthengine-api`。它计算指定区域内，年度 AEF 的 A00–A63 全部波段均未被掩膜的面积占比，并保存年度统计、源资产清单、实际区域边界和汇总 CSV。

## 运行准备

```powershell
python -m pip install earthengine-api
earthengine authenticate
```

已有登录状态不需要重新认证。查询命令可追加 `--project YOUR_GEE_PROJECT`；已有凭据配置了 Project 时可省略。脚本不自动启动交互登录。

以下命令在 AEF-GRiTS 目录运行。也可以单独复制 Python 文件到其他位置，再直接调用它。
必须指定区域、年份和输出目录；将示例中的输出位置换成自己的数据目录。

## 指定区域

三种方式互斥，不能同时使用。

```powershell
# 行政区：GAUL 2015 精确名称；国家、省、市对应 level0、level1、level2。
python -X utf8 scripts/check_aef_coverage.py `
  --country China --admin1 "Hubei Sheng" --admin2 Wuhan `
  --years 2023 2024 2025 --crs EPSG:32650 `
  --out-dir ./outputs/coverage/wuhan_new

# 自定义区域：WGS84 GeoJSON，多个面要素取并集，保留孔洞。
python -X utf8 scripts/check_aef_coverage.py `
  --aoi region.geojson --years 2024 2025 `
  --out-dir ./outputs/coverage/region_new

# 经纬度矩形：WEST SOUTH EAST NORTH。
python -X utf8 scripts/check_aef_coverage.py `
  --bbox 113.6 30.0 115.1 31.4 --years 2025 `
  --out-dir ./outputs/coverage/bbox_new
```

只传 `--country` 可选择国家；增加 `--admin1` 选择省级区域；增加 `--admin2` 选择二级区域。名称按 GAUL 字段精确匹配，同名/无匹配均报错，不会静默选择另一个区域。`--admin2` 要求同时提供国家及一级区域。

GeoJSON 支持 Polygon、MultiPolygon、Feature 和 FeatureCollection，坐标为 WGS84 经纬度。投影坐标、点或线、空几何以及未闭合环会被拒绝；Shapefile/GeoPackage 需先导出为 WGS84 面状 GeoJSON。bbox 不支持直接跨越 180° 经线，可用拆分后的多面 GeoJSON 表示该区域。

## 统计口径

1. 按每年 `[01-01, 次年01-01)` 及区域筛选 `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`。
2. 选择 A00–A63 全部 64 波段，mosaic 后对 mask 取最小值。
3. 用 `unmask(0, sameFootprint=False)` 将影像覆盖外的位置明确计为缺失。
4. 在同一网格上对整个区域的 `pixelArea`、有效面积、缺失面积求和。总面积分母不受 AEF 的掩膜影响。
5. 检验有效面积与缺失面积之和等于总面积，然后计算覆盖率。

默认分辨率 `--scale 10` 米，默认 CRS 为 `EPSG:6933`。可指定适合研究区域的 `--crs`；`scale` 按 Earth Engine 的米尺度参数传递，即使使用经纬度 CRS，也不会误把 10 米当成 10 度。[官方参数说明](https://developers.google.com/earth-engine/apidocs/ee-image-reduceregion)。多要素区域通过集合 geometry 合并，[官方说明](https://developers.google.com/earth-engine/apidocs/ee-featurecollection-geometry)。

不使用 `bestEffort` 自动降分辨率。大区域可显式设置如 `--scale 100`，此时结果就是 100 米统计，不代表逐个 10 米像元检查。可调整 `--max-pixels`（默认 20 亿）和 `--timeout-seconds`（默认 180 秒）；服务端资源限制仍可能导致请求失败。

这是源 **mask 覆盖率**，不是本地下载完整性检查，也不检查源浮点数中未被 mask 标记的 NaN。请求失败、没有源影像、空响应或异常面积会报错，不会冒充完整覆盖。

## 输出

```text
输出目录/
  aoi.geojson
  2024/coverage_2024.json
  2025/coverage_2025.json
  coverage_summary.csv
```

年度 JSON 包含区域来源、资产 ID/版本、年份区间、统计 CRS/分辨率、总面积/有效面积/缺失面积及百分比。每年成功即保存该年回执，全部年份完成才输出汇总 CSV。建议新查询使用新输出目录，保留历史结果。

本次通用化经过 51 项离线测试，覆盖区域选择、输入几何、尺度参数、面积计算及错误处理；没有重新运行历史武汉全市查询。

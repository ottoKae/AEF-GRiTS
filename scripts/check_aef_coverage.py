#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""查询任意指定区域的 AEF 年度源掩膜覆盖率（可独立复制运行）。

依赖：Python 3.10+，earthengine-api；无需安装 AEF-GRiTS。
安装：python -m pip install earthengine-api
认证：先运行 earthengine authenticate，再配置自己的 Earth Engine Project。

示例（按需追加 --project YOUR_GEE_PROJECT）：
  # 按 GAUL 行政区精确名称筛选，可查国家、省或市。
  python check_aef_coverage.py --country China --admin1 "Hubei Sheng" --admin2 Wuhan --years 2023 2024 2025 --crs EPSG:32650 --out-dir ./outputs/coverage/wuhan_new
  # 自定义 WGS84 Polygon/MultiPolygon GeoJSON；多个要素取并集。
  python check_aef_coverage.py --aoi region.geojson --years 2025 --out-dir YOUR_OUTPUT_DIR
  # 经纬度矩形：西、南、东、北；不支持跨越 180 度经线的 bbox。
  python check_aef_coverage.py --bbox 113.6 30.0 115.1 31.4 --years 2025 --out-dir YOUR_OUTPUT_DIR
已有凭据中保存了 Project 时，可以省略 --project。
必须显式提供 --out-dir，建议每批使用新目录；在本工作区应放入统一 AEF 存储。
Windows 中文终端可使用 python -X utf8 运行。

检查过程：
1. 按行政区、GeoJSON 或 bbox 定义统计区域，不把多边形替换为外接矩形。
2. 按年份和区域筛选 GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL，选择 A00–A63。
3. mosaic 后对 64 个波段的 mask 取最小值：全部波段有效才算有效像元。
4. 将无影像覆盖位置的 mask 明确补为 0，避免从统计中漏掉缺失区域。
5. 在指定 CRS、分辨率上分别统计总面积、有效面积、缺失面积。
6. 覆盖率 = 有效面积 / 总面积 × 100%；保存逐年原始回执和汇总 CSV。

口径：检查 GEE 源 mask，不能替代本地文件完整性或实际浮点 NaN 检查。
默认 EPSG:6933、10 m；可用 --crs / --scale 指定，scale 始终以米为单位。
GAUL 边界版本是 2015；源 mask 以最近邻方式查询到统一网格。
未使用 bestEffort 自动降精度；大区域可能超时，可显式修改分辨率并记录口径。
pixelArea 栅格面积与行政边界 geometry.area() 不应混用作分子/分母。
不下载特征栅格。任何请求失败都会抛错，不会把失败当作 0 缺失。
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time

DATASET = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
BANDS = [f"A{i:02d}" for i in range(64)]
ASSET_PROPERTIES = [
    "system:index", "system:time_start", "system:time_end", "UTM_ZONE",
    "DATASET_VERSION", "MODEL_VERSION", "PROCESSING_SOFTWARE_VERSION",
]


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def coverage_percentages(stats: dict) -> tuple[float, float]:
    """拒绝空响应/NaN/负面积；不把计算失败误报为全覆盖。"""
    names = ("total_m2", "valid_m2", "missing_m2")
    if any(stats.get(name) is None for name in names):
        raise ValueError(f"面积统计不完整：{stats}")
    total, valid, missing = (float(stats[name]) for name in names)
    if not all(math.isfinite(value) and value >= 0 for value in (total, valid, missing)):
        raise ValueError(f"面积包含非有限值或负数：{stats}")
    if total <= 0 or abs(total - valid - missing) > 1.0:
        raise ValueError(f"总面积必须为正，且有效面积 + 缺失面积 = 总面积：{stats}")
    if valid > total or missing > total:
        raise ValueError(f"分项面积超过总面积：{stats}")
    return 100 * valid / total, 100 * missing / total


def admin_selection(args) -> tuple[str, dict]:
    """按最细的行政层级选择 GAUL 集合，并使用精确匹配避免同名误选。"""
    filters = {"ADM0_NAME": args.country}
    if args.admin1 is not None:
        filters["ADM1_NAME"] = args.admin1
    if args.admin2 is not None:
        filters["ADM2_NAME"] = args.admin2
    return f"FAO/GAUL/2015/level{len(filters) - 1}", filters


def load_geojson(path: Path) -> dict:
    """规范化面状 WGS84 GeoJSON；不猜测或静默转换输入坐标系。"""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("GeoJSON 必须是对象")
    kind = payload.get("type")
    if kind == "FeatureCollection":
        features = payload.get("features", [])
    elif kind == "Feature":
        features = [payload]
    else:
        features = [{"type": "Feature", "properties": {}, "geometry": payload}]
    if not isinstance(features, list) or not features:
        raise ValueError("GeoJSON 没有面要素")
    allowed_crs = {"epsg:4326", "urn:ogc:def:crs:epsg::4326",
                   "crs84", "ogc:crs84", "urn:ogc:def:crs:ogc:1.3:crs84"}
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise ValueError("FeatureCollection 必须只包含 Feature")
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError("仅支持非空 Polygon/MultiPolygon 面区域")
        for obj in (payload, feature, geometry):
            crs = obj.get("crs")
            if crs is not None:
                name = crs.get("properties", {}).get("name", "") if isinstance(crs, dict) else ""
                if str(name).lower() not in allowed_crs:
                    raise ValueError("GeoJSON 必须使用 WGS84 经纬度（EPSG:4326/CRS84）")
        coordinates = geometry.get("coordinates", [])
        polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
        if not isinstance(polygons, list) or not polygons:
            raise ValueError("GeoJSON 面坐标为空")
        for polygon in polygons:
            if not isinstance(polygon, list) or not polygon:
                raise ValueError("GeoJSON 面必须有外环")
            for ring in polygon:
                if not isinstance(ring, list) or len(ring) < 4:
                    raise ValueError("GeoJSON 环至少需要 4 个坐标，且首尾闭合")
                for point in ring:
                    if (not isinstance(point, list) or len(point) < 2
                            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                   or not math.isfinite(v) for v in point[:2])
                            or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90):
                        raise ValueError("GeoJSON 坐标必须是有效 WGS84 经纬度")
                if ring[0][:2] != ring[-1][:2]:
                    raise ValueError("GeoJSON 环的首尾坐标必须相同")
    return {"type": "FeatureCollection", "features": features}


def resolve_region(ee, args, aoi_geojson: dict | None = None):
    if args.country:
        dataset, filters = admin_selection(args)
        candidates = ee.FeatureCollection(dataset)
        for field, value in filters.items():
            candidates = candidates.filter(ee.Filter.eq(field, value))
        geojson = candidates.getInfo()
        count = len(geojson.get("features", []))
        if count != 1:
            raise ValueError(f"行政区筛选 {filters} 匹配 {count} 个要素；请核对 GAUL 名称/层级")
        roi = candidates.geometry(maxError=1)
        source = {"type": "gaul", "dataset": dataset, "filters": filters,
                  "properties": geojson["features"][0]["properties"]}
    else:
        if args.aoi:
            geojson = aoi_geojson
            source = {"type": "geojson", "path": str(args.aoi.resolve())}
        else:
            west, south, east, north = args.bbox
            geojson = {"type": "FeatureCollection", "features": [{
                "type": "Feature", "properties": {}, "geometry": {
                    "type": "Polygon", "coordinates": [[[west, south], [east, south],
                        [east, north], [west, north], [west, south]]],
                },
            }]}
            source = {"type": "bbox", "bounds_wgs84": args.bbox}
        # GeoJSON 的边在经纬度平面连接；多个要素合并后避免重复计面积。
        features = [ee.Feature(ee.Geometry(f["geometry"], proj="EPSG:4326", geodesic=False),
                               f.get("properties") or {}) for f in geojson["features"]]
        roi = ee.FeatureCollection(features).geometry(maxError=1)
    geometry_info = ee.Dictionary({
        "boundary_area_m2": roi.area(maxError=1),
        "boundary_bounds": roi.bounds(maxError=1).coordinates(),
    }).getInfo()
    area = geometry_info.get("boundary_area_m2")
    if area is None or not math.isfinite(area) or area <= 0:
        raise ValueError("区域面积必须为正；请核对输入几何")
    return roi, geojson, {"boundary_source": source,
                           "boundary_feature_count": len(geojson["features"]), **geometry_info}


def query_year(ee, roi, boundary_info: dict, year: int, *, crs: str, scale: float,
               max_pixels: int) -> dict:
    started = time.monotonic()
    collection = (
        ee.ImageCollection(DATASET)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .filterBounds(roi)
        .select(BANDS)
    )
    print(f"[{year}] 查询源影像清单……", flush=True)
    inventory = ee.Dictionary({
        "asset_count": collection.size(),
        "assets": collection.toList(collection.size()).map(
            lambda item: ee.Image(item).toDictionary(ASSET_PROPERTIES)
        ),
    }).getInfo()
    if inventory["asset_count"] == 0:
        raise RuntimeError(f"{year} 年没有检索到 AEF 源影像，不能据此宣称统计成功")

    # 关键：取全部 64 个波段的 mask，不仅检查影像外框或一个波段。
    valid = (
        collection.mosaic().mask().reduce(ee.Reducer.min())
        .unmask(0, sameFootprint=False).gt(0)
    )
    # 分母独立于 AEF mask。无覆盖位置仍属于总面积，同时计入 missing_m2。
    area = ee.Image.pixelArea()
    areas = (
        area.rename("total_m2")
        .addBands(area.multiply(valid).rename("valid_m2"))
        .addBands(area.multiply(valid.Not()).rename("missing_m2"))
    )
    print(f"[{year}] 对 {inventory['asset_count']} 幅源影像进行区域 {scale:g} m 统计……", flush=True)
    stats = areas.reduceRegion(
        reducer=ee.Reducer.sum(),
        geometry=roi,
        crs=crs,
        scale=scale,  # EE 将 scale 解释为米；不能直接把它用作经纬度 CRS 的仿射步长。
        maxPixels=max_pixels,
        bestEffort=False,  # 不允许为缩短计算而自动改成粗分辨率。
        tileScale=4,
    ).getInfo()
    valid_percent, missing_percent = coverage_percentages(stats)
    return {
        "status": "complete",
        "queried_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": DATASET,
        "year": year,
        "date_range": [f"{year}-01-01", f"{year + 1}-01-01"],
        **boundary_info,
        **inventory,
        "statistics_crs": crs,
        "statistics_scale_m": scale,
        "max_pixels": max_pixels,
        "best_effort": False,
        "mask_definition": "All 64 bands unmasked in the annual mosaic; nearest-neighbor sampling at the recorded CRS and metre scale",
        "scope": "Source mask coverage; not local-file integrity or floating-point NaN validation",
        "coverage": stats,
        "valid_percent": valid_percent,
        "missing_percent": missing_percent,
        "elapsed_seconds": time.monotonic() - started,
    }


def write_summary(reports: list[dict], path: Path) -> None:
    fields = ["year", "source_images", "coverage_percent", "missing_percent",
              "total_km2", "valid_km2", "missing_km2"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for report in reports:
            valid_percent, missing_percent = coverage_percentages(report["coverage"])
            writer.writerow({
                "year": report["year"],
                "source_images": report["asset_count"],
                "coverage_percent": valid_percent,
                "missing_percent": missing_percent,
                **{name + "_km2": report["coverage"][name + "_m2"] / 1e6
                   for name in ("total", "valid", "missing")},
            })


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    region = parser.add_mutually_exclusive_group(required=True)
    region.add_argument("--country", help="GAUL ADM0_NAME 精确名称，如 China")
    region.add_argument("--aoi", type=Path, help="WGS84 面状 GeoJSON 文件")
    region.add_argument("--bbox", nargs=4, type=float, metavar=("WEST", "SOUTH", "EAST", "NORTH"))
    parser.add_argument("--admin1", help="GAUL ADM1_NAME 精确名称，需同时指定 --country")
    parser.add_argument("--admin2", help="GAUL ADM2_NAME 精确名称，需同时指定 --country/--admin1")
    parser.add_argument("--years", nargs="+", type=int, required=True)
    parser.add_argument("--crs", default="EPSG:6933", help="统计 CRS，默认 EPSG:6933 等面积投影")
    parser.add_argument("--scale", type=float, default=10, help="统计分辨率（米），默认 10；不自动降精度")
    parser.add_argument("--max-pixels", type=int, default=2_000_000_000)
    parser.add_argument("--timeout-seconds", type=float, default=180)
    parser.add_argument("--project", default=os.environ.get("AEF_GRITS_PROJECT"),
                        help="Earth Engine Project；省略时复用已有凭据的 Project")
    parser.add_argument("--out-dir", type=Path, required=True, help="统计回执输出目录，建议每批使用新目录")
    args = parser.parse_args(argv)
    args.years = sorted(set(args.years))
    if any(year < 2017 or year > 9998 for year in args.years):
        parser.error("年份必须不早于 2017，且可构造次年日期")
    if (args.admin1 is not None or args.admin2 is not None) and not args.country:
        parser.error("--admin1/--admin2 只能与 --country 一起使用")
    if args.admin2 is not None and args.admin1 is None:
        parser.error("--admin2 需要 --admin1，以避免同名行政区误选")
    for name in ("country", "admin1", "admin2"):
        value = getattr(args, name)
        if value is not None:
            if not value.strip():
                parser.error(f"--{name} 不能为空")
            setattr(args, name, value.strip())
    if args.bbox is not None:
        west, south, east, north = args.bbox
        if (not all(math.isfinite(v) for v in args.bbox)
                or not -180 <= west < east <= 180 or not -90 <= south < north <= 90):
            parser.error("bbox 必须满足 -180≤西<东≤180，-90≤南<北≤90；跨180度请拆成GeoJSON多面")
    if not math.isfinite(args.scale) or args.scale <= 0:
        parser.error("--scale 必须是有限正数，单位为米")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0 or args.max_pixels <= 0:
        parser.error("--timeout-seconds 和 --max-pixels 必须为正")
    return args


def main() -> None:
    args = parse_args()
    # 本地输入先验证，不因错误文件触发认证或服务端请求。
    aoi_geojson = load_geojson(args.aoi) if args.aoi else None

    import ee  # 延迟导入，使 --help 不依赖登录或网络。

    ee.Initialize(project=args.project or None)  # 使用已有凭据；不自动弹出登录。
    ee.data.setDeadline(args.timeout_seconds * 1000)
    ee.data.setMaxRetries(0)  # 请求失败直接抛错；不会用空统计结果替代。
    print("解析统计区域……", flush=True)
    roi, geojson, boundary_info = resolve_region(ee, args, aoi_geojson)
    save_json(args.out_dir / "aoi.geojson", geojson)

    reports = []
    for year in args.years:
        report = query_year(ee, roi, boundary_info, year, crs=args.crs, scale=args.scale,
                            max_pixels=args.max_pixels)
        save_json(args.out_dir / str(year) / f"coverage_{year}.json", report)
        reports.append(report)
        print(f"[{year}] 覆盖率 {report['valid_percent']:.9f}%；"
              f"缺失面积 {report['coverage']['missing_m2']:.6f} m²", flush=True)
    write_summary(reports, args.out_dir / "coverage_summary.csv")
    print(f"结果已保存：{args.out_dir.resolve()}", flush=True)


if __name__ == "__main__":
    main()

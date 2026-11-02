"""Small real CLI regression against the frozen native-grid audit evidence."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import numpy as np
import pandas as pd
import rasterio
from affine import Affine
from pyproj import Transformer

from aef_grits.atomic import write_json
from aef_grits.earth_engine import AEF_BANDS, feature_columns
from aef_grits.grids import Tessera01GridProvider
from aef_grits.native_grid import NATIVE_PROTOCOL
from aef_grits.store import AEFZarr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    result = {'started_utc': datetime.now(timezone.utc).isoformat(),
              'protocol': NATIVE_PROTOCOL, 'historical_data': 'read-only',
              'cli_runs': [], 'points': {}, 'grids': []}
    report = args.out / 'verification.json'
    if report.exists():
        result = json.loads(report.read_text(encoding='utf-8'))

    def run(label, module, arguments, expected=0):
        attempts = [row for row in result['cli_runs'] if row['case'] == label]
        if attempts and attempts[-1]['returncode'] == expected:
            log = args.out / attempts[-1].get('log', f'{label}.log')
            return subprocess.CompletedProcess([], expected, log.read_text(encoding='utf-8'), '')
        started = time.monotonic()
        proc = subprocess.run([sys.executable, '-B', '-m', module, *map(str, arguments)],
                              capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=300)
        log = args.out / f'{label}_attempt{len(attempts) + 1}.log'
        log.write_text(proc.stdout + proc.stderr, encoding='utf-8')
        result['cli_runs'].append({'case': label, 'returncode': proc.returncode,
                                  'log': log.name,
                                  'seconds': round(time.monotonic() - started, 3)})
        write_json(result, report)
        print(f'{label}: exit={proc.returncode}', flush=True)
        assert (proc.returncode == 0) == (expected == 0), f'See {label}.log'
        return proc

    points = pd.read_parquet(args.audit / 'historical_point_coordinates.parquet')
    point_input = args.out / 'points.parquet'
    points[['sample_id', 'lon', 'lat']].to_parquet(point_input, index=False)
    shared = ['--years', 2017, 2025, '--workers', 1, '--max-retries', 1,
              '--request-timeout-seconds', 60]
    for size in (4, 1):
        run(f'points_chunk{size}', 'scripts.stream_aef_points_ee',
            ['--samples', point_input, '--out-dir', args.out / f'points_chunk{size}',
             '--chunk-size', size, *shared])
    frames = [pd.concat([pd.read_parquet(p) for p in sorted(
        (args.out / f'points_chunk{size}' / 'shards').glob('*.parquet'))]).set_index('sample_id').sort_index()
        for size in (4, 1)]
    columns = feature_columns([2017, 2025])
    assert np.array_equal(frames[0][columns], frames[1][columns], equal_nan=True)
    native = pd.read_parquet(args.audit / '2025/four_points_native_0.parquet').set_index('sample_id')
    actual = frames[0][feature_columns([2025])].to_numpy(np.float32)
    expected = native.loc[frames[0].index, list(AEF_BANDS)].to_numpy(np.float32)
    assert np.array_equal(actual, expected)
    result['points'] = {'count': len(points), 'years': [2017, 2025],
                        'chunk_1_vs_4_exact': True, 'native_2025_exact_values': int(actual.size)}
    run('points_resume', 'scripts.stream_aef_points_ee',
        ['--samples', point_input, '--out-dir', args.out / 'points_chunk4',
         '--chunk-size', 4, *shared])
    summary = json.loads((args.audit / 'summary.json').read_text())
    cases = []
    for old in summary['grid_cases']:
        store = AEFZarr(old['local_store'])
        row, col = store.height // 2 - 8, store.width // 2 - 8
        transform = store.transform * Affine.translation(col, row)
        expected = store.read_window(row, row + 16, col, col + 16, years=[old['year']])[0]
        cases.append((old['case'], old['year'], store.crs, transform, expected))
    for label, lon in (('zone49', 113.9999), ('zone50', 114.0001)):
        grid = Tessera01GridProvider().get(lon, 30.55)
        x, y = Transformer.from_crs(4326, grid.crs, always_xy=True).transform(lon, 30.55)
        col, row = np.floor(~grid.transform * (x, y)).astype(int)
        col = max(0, min(col - 8, grid.width - 16))
        row = max(0, min(row - 8, grid.height - 16))
        transform = grid.transform * Affine.translation(col, row)
        sources = json.loads((args.audit / 'native_footprint_verification_v2.json').read_text())
        case = next(c for c in sources['zone_boundary'] if c['label'] == label)
        source = next(s for s in case['sources'] if s['source_crs'] == grid.crs)
        key = hashlib.sha256(source['id'].encode()).hexdigest()[:16]
        expected = np.moveaxis(np.load(args.audit / f'2025/boundary_{label}_{key}.npy'), -1, 0)
        cases.append((label, 2025, grid.crs, transform, expected))
    for label, year, crs, transform, expected in cases:
        reference = args.out / f'{label}_reference.tif'
        with rasterio.open(reference, 'w', driver='GTiff', width=16, height=16,
                           count=1, dtype='uint8', crs=crs, transform=transform) as raster:
            raster.write(np.zeros((1, 16, 16), np.uint8))
        out = args.out / label
        arguments = ['--reference', reference, '--tile-id', label, '--out-dir', out,
                     '--years', year, '--block-size', 16, '--workers', 1,
                     '--max-retries', 1, '--request-timeout-seconds', 60]
        tiff_only = label == 'zone50'
        arguments += ['--geotiff-only' if tiff_only else '--export-geotiff']
        run(label, 'scripts.stream_aef_grid_ee', arguments)
        manifest = json.loads((out / 'geotiff_manifest.json').read_text())
        entry = manifest['grids'][0]
        with rasterio.open(entry['files'][0]['path']) as raster:
            actual = raster.read()
            assert str(raster.crs) == str(crs) and raster.transform == transform
        assert np.array_equal(actual, expected, equal_nan=True), label
        if not tiff_only:
            path = next(out.glob('**/*.zarr'))
            data = AEFZarr(path).read_window(0, 16, 0, 16, years=[year])[0]
            assert np.array_equal(data, expected, equal_nan=True), label
        result['grids'] = [row for row in result['grids'] if row['case'] != label]
        result['grids'].append({'case': label, 'crs': str(crs), 'year': year,
                                'exact_values': int(actual.size),
                                'native_source_signature': entry['native_source_manifest']['signature'],
                                'tiff_only': tiff_only})
        write_json(result, report)
        if label == 'zone50':
            run('tiff_only_resume', 'scripts.stream_aef_grid_ee', arguments)
    reference = args.out / 'shifted_reference.tif'
    with rasterio.open(reference, 'w', driver='GTiff', width=16, height=16, count=1,
                       dtype='uint8', crs=crs,
                       transform=Affine(transform.a, 0, transform.c + 3, 0, transform.e, transform.f)) as raster:
        raster.write(np.zeros((1, 16, 16), np.uint8))
    rejected = run('shifted_rejection', 'scripts.stream_aef_grid_ee',
                  ['--reference', reference, '--tile-id', 'shifted', '--out-dir', args.out / 'shifted',
                   '--years', 2025, '--workers', 1, '--max-retries', 1,
                   '--request-timeout-seconds', 60], expected=1)
    assert 'No native AEF source matches' in rejected.stdout + rejected.stderr
    assert not list((args.out / 'shifted').glob('**/*.zarr'))
    result.update(status='passed', shifted_grid_rejected=True,
                  finished_utc=datetime.now(timezone.utc).isoformat())
    write_json(result, report)


if __name__ == '__main__':
    main()

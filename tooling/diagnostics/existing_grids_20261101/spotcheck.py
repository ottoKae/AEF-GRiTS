"""Read-only 16x16 native-source checks for explicitly selected historical grids."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
from affine import Affine
import rasterio

from aef_grits.atomic import write_json
from aef_grits.earth_engine import initialize
from aef_grits.native_grid import iter_native_sources, native_grid_image, NativeAlignmentError
from aef_grits.store import AEFZarr
from tooling.diagnostics.native_alignment_20261101.audit import Audit, compare


def classify(local, native, foreign):
    valid = np.isfinite(local).all(axis=-1)
    if not valid.any():
        return 'inconclusive_local_nodata'
    if not np.isfinite(native).all(axis=-1).any():
        return 'inconclusive_native_nodata'
    if np.array_equal(local, native, equal_nan=True):
        return 'sample_native_match'
    differs = ~np.equal(local, native).all(axis=-1)
    if any((np.equal(local, other).all(axis=-1) & valid & differs).any() for other in foreign):
        return 'cross_utm_source_match_at_differing_pixels'
    return 'unresolved_difference_not_automatically_resampling'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--high-volume', action='store_true')
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    args.out.mkdir(parents=True, exist_ok=True)
    ee, auth = initialize(None, request_timeout_seconds=60, return_auth=True,
                          high_volume=args.high_volume)
    audit = Audit(ee, args.out)
    result = {'started_utc': datetime.now(timezone.utc).isoformat(),
              'authentication': auth.public_summary(), 'historical_data': 'read-only',
              'scope': 'selected 16x16 windows, not whole-product certification',
              'high_volume_endpoint': args.high_volume,
              'request_attempt_budget': 60, 'cases': []}
    for case in cases:
        label, year, path = case['id'], int(case['year']), Path(case['path'])
        row, col = int(case['row']), int(case['column'])
        if path.suffix.lower() == '.zarr':
            store = AEFZarr(path)
            crs, transform = store.crs, store.transform * Affine.translation(col, row)
            local = np.moveaxis(store.read_window(row, row + 16, col, col + 16, years=[year])[0], 0, -1)
        else:
            with rasterio.open(path) as store:
                crs, transform = str(store.crs), store.transform * Affine.translation(col, row)
                local = np.moveaxis(store.read(window=rasterio.windows.Window(col, row, 16, 16), masked=True)
                                   .astype(np.float32).filled(np.nan), 0, -1)
        if local.shape != (16, 16, 64):
            raise ValueError(f'{label}: select an interior 16x16 window')
        corners = np.asarray([transform * xy for xy in [(0, 0), (16, 0), (0, 16), (16, 16)]])
        bounds = [*corners.min(axis=0), *corners.max(axis=0)]
        geometry = ee.Geometry.Rectangle(bounds, crs, False)
        sources = list(iter_native_sources(ee, year, geometry,
                       lambda op, fn: audit.fetch(f'metadata/{label}_{op.replace(":", "_")}', fn)))
        entry = {**case, 'crs': crs, 'transform': list(transform)[:6],
                 'sources': [source.as_dict() for source in sources]}
        np.save(args.out / f'{label}_local.npy', local)
        try:
            image, manifest = native_grid_image(ee, sources, crs, transform)
        except NativeAlignmentError as exc:
            entry.update(status='no_compatible_native_source', reason=str(exc))
        else:
            native = audit.pixels(f'{label}_native', image, crs, transform)
            entry['native_comparison'] = compare(local, native)
            entry['manifest'] = manifest
            foreign, contributions = [], []
            for index, source in enumerate(sources):
                if source.crs != crs:
                    other = audit.pixels(f'{label}_foreign{index}', source.image(ee), crs, transform)
                    foreign.append(other)
                    contributions.append({'source_id': source.id, 'source_crs': source.crs,
                                          'comparison_on_target_grid': compare(local, other)})
            entry['foreign_source_checks'] = contributions
            entry['status'] = classify(local, native, foreign)
        result['cases'].append(entry)
        result['request_attempts'] = len(audit.events)
        write_json(result, args.out / 'spotchecks.json')
        print(f'{label}: {entry["status"]}', flush=True)
    result['finished_utc'] = datetime.now(timezone.utc).isoformat()
    write_json(result, args.out / 'spotchecks.json')


if __name__ == '__main__':
    main()

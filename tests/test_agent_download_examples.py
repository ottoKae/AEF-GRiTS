"""Run the skill's offline examples against the real CLI and isolated tiny inputs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / '.agents/skills/aef-grits-download/references/commands.md'
COMMANDS = [line for line in REFERENCE.read_text(encoding='utf-8').splitlines()
            if line.startswith('python -m scripts.stream_aef_')
            and ('--plan-only' in line or '--validate-only' in line)]


@pytest.mark.parametrize('command', COMMANDS)
def test_skill_offline_examples(command, tmp_path):
    samples = tmp_path / 'samples.csv'
    samples.write_text('sample_id,lon,lat\np1,114.05,30.55\np2,114.06,30.56\n', encoding='utf-8')
    reference = tmp_path / 'reference.tif'
    with rasterio.open(reference, 'w', driver='GTiff', height=2, width=2, count=1,
                       dtype='uint8', crs='EPSG:32650', transform=from_origin(500000, 3400000, 10, 10)) as dst:
        dst.write(np.ones((1, 2, 2), dtype=np.uint8))
    paths = {'SAMPLES.csv': samples, 'REFERENCE.tif': reference,
             'OUTPUT_DIR': tmp_path / 'output', 'STATE_DIR': tmp_path / 'state',
             'RESOURCE_DIR': tmp_path / 'resources'}
    args = [str(paths.get(arg, arg)) for arg in shlex.split(command)[1:]]
    env = {**os.environ, 'PYTHONUTF8': '1',
           'GOOGLE_APPLICATION_CREDENTIALS': str(tmp_path / 'no-cloud-credential.json')}
    result = subprocess.run([sys.executable, '-X', 'utf8', *args], cwd=ROOT,
                            env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads((paths['STATE_DIR'] / 'validation_report.json').read_text(encoding='utf-8')
                        if '--validate-only' in args else result.stdout)
    assert report['years'] == [2025]
    if '--validate-only' in args:
        assert report['rows'] == 2
        assert report['feature_count'] == 64
        assert report['estimated_shards'] == 1
        assert (paths['STATE_DIR'] / 'validation_report.json').is_file()
        assert not (paths['OUTPUT_DIR'] / 'shards').exists()
    else:
        assert report['grid_count'] == 1
        assert report['grids'][0]['crs'].startswith('EPSG:326')
        assert report['raw_bytes'] > 0
        assert not Path(report['grids'][0]['output']).exists()

import pytest

import webapp.app as web


def test_local_auth_requires_same_origin_and_local_mode(monkeypatch):
    client = web.app.test_client()
    assert client.post('/api/local-auth/start', json={'action': 'login'}).status_code == 400
    assert client.post('/api/local-auth/start', json={'action': 'login'}, headers={'Origin': 'https://untrusted.example'}).status_code == 403
    monkeypatch.setattr(web._web_auth, 'mode', 'oauth')
    assert client.post('/api/local-auth/start', json={'action': 'login'}, headers={'Origin': 'http://localhost'}).status_code != 202


def test_local_login_blocked_while_download_queued(monkeypatch):
    monkeypatch.setattr(web, '_tasks', {'existing': {'status': 'queued'}})
    result = web.app.test_client().post('/api/local-auth/start', json={'action': 'login'}, headers={'Origin': 'http://localhost'})
    assert result.status_code == 400
    assert 'cancel' in result.json['error']


def test_local_verified_status_binds_requested_project(monkeypatch):
    monkeypatch.setattr(web._local_auth, 'status', lambda: {'state': 'done', 'verified': True})
    monkeypatch.setattr(web._local_auth, 'is_verified', lambda project: project == 'chosen-test')
    client = web.app.test_client()
    for project, expected in [('chosen-test', True), ('other-test', False)]:
        result = client.post('/api/local-auth/status', json={'project': project}, headers={'Origin': 'http://localhost'})
        assert result.json['verified'] is expected


@pytest.mark.parametrize('output_format,flag', [('zarr+geotiff', '--export-geotiff'), ('geotiff', '--geotiff-only')])
def test_format_plan_command_and_extra_disk_budget(tmp_path, output_format, flag):
    params = web._normalize_params({'project': 'test-project', 'workflow': 'grid',
                                   'years': [2025], 'bbox': [117.01, 31.01, 117.09, 31.09],
                                   'outputFormat': output_format})
    plan = web._plan_grid(params)
    assert plan['output_format'] == output_format
    command = web._build_cmd(params, tmp_path)
    assert command[1:3] == ['-m', 'scripts.stream_aef_grid_ee']
    assert flag in command
    with_tiff = web._capacity_plan(plan)
    zarr_only = web._capacity_plan({**plan, 'output_format': 'zarr'})
    assert with_tiff['disk_required_bytes'] - zarr_only['disk_required_bytes'] == plan['raw_bytes']
    with pytest.raises(ValueError, match='Point outputs'):
        web._normalize_params({'project': 'test-project', 'workflow': 'points', 'years': [2025], 'outputFormat': 'zarr+geotiff'})


def test_unimplemented_public_download_fails_explicitly():
    with pytest.raises(ValueError, match='experimental'):
        web._normalize_params({'workflow': 'grid', 'dataSource': 'public'})


def test_login_change_invalidates_prepared_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'PLANS_DIR', tmp_path / 'plans')
    monkeypatch.setattr(web, 'OUTPUT_ROOT', tmp_path)
    params = {'project': 'chosen-test', 'workflow': 'points', 'years': [2025]}
    with web.app.test_request_context():
        prepared = web._write_plan(params, {'raw_bytes': 1})
        web._read_plan(prepared['plan_id'])
        monkeypatch.setattr(web, '_local_auth_epoch', 'new-login')
        with pytest.raises(ValueError, match='authentication'):
            web._read_plan(prepared['plan_id'])


def test_native_folder_selection_binds_plan_and_checks_selected_disk(tmp_path, monkeypatch):
    selected = tmp_path / 'different disk 中文'
    selected.mkdir()
    monkeypatch.setattr(web, 'pick_directory', lambda initial: str(selected))
    monkeypatch.setattr(web, 'OUTPUT_ROOT', tmp_path / 'configured')
    monkeypatch.setattr(web, 'PLANS_DIR', tmp_path / 'plans')
    checked = []
    monkeypatch.setattr(web, 'isolated_disk_free', lambda path, **kw: checked.append(path) or 100 * 1024**3)
    client = web.app.test_client()
    result = client.post('/api/output-directories/pick', headers={'Origin': 'http://localhost'})
    assert result.status_code == 200
    assert result.json['path'] == str(selected.resolve())
    with web.app.test_request_context():
        params = web._normalize_params({'project': 'test-project', 'workflow': 'points', 'years': [2025],
                                       'outputDirectoryToken': result.json['token']})
        prepared = web._write_plan(params, {'raw_bytes': 1})
        _, record = web._read_plan(prepared['plan_id'])
    assert checked == [selected.resolve(), selected.resolve()]
    assert record['params']['output_root'] == str(selected.resolve())
    assert web._safe_output_dir('', 'job1', selected_root=params['output_root']) == selected.resolve() / 'job1'
    assert web.OUTPUT_ROOT != selected
    with pytest.raises(ValueError, match='selection'):
        web._normalize_params({'project': 'test-project', 'workflow': 'points', 'years': [2025],
                               'outputDirectoryToken': 'not-a-valid-grant'})


def test_native_folder_requires_local_origin_and_loopback(monkeypatch):
    calls = []
    monkeypatch.setattr(web, 'pick_directory', lambda initial: calls.append(initial))
    client = web.app.test_client()
    assert client.post('/api/output-directories/pick').status_code == 403
    assert client.post('/api/output-directories/pick', headers={'Origin': 'https://remote.example'}).status_code == 403
    assert client.post('/api/output-directories/pick', headers={'Origin': 'http://localhost'},
                       environ_overrides={'REMOTE_ADDR': '192.0.2.1'}).status_code == 403
    monkeypatch.setattr(web._web_auth, 'mode', 'token')
    assert client.post('/api/output-directories/pick', headers={'Origin': 'http://localhost'}).status_code == 403
    assert not calls


def test_native_folder_cancel_or_unavailable_does_not_change_configuration(monkeypatch):
    original = web.OUTPUT_ROOT
    monkeypatch.setattr(web, 'pick_directory', lambda initial: None)
    client = web.app.test_client()
    result = client.post('/api/output-directories/pick', headers={'Origin': 'http://localhost'})
    assert result.json == {'cancelled': True}
    def unavailable(initial):
        raise RuntimeError('No desktop; use the web folder browser')
    monkeypatch.setattr(web, 'pick_directory', unavailable)
    result = client.post('/api/output-directories/pick', headers={'Origin': 'http://localhost'})
    assert result.status_code == 400 and 'web folder browser' in result.json['error']
    assert web.OUTPUT_ROOT == original

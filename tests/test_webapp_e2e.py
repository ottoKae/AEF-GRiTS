from __future__ import annotations

from pathlib import Path
import os
import threading

import pytest
from werkzeug.serving import make_server

import webapp.app as web


CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
playwright = pytest.importorskip("playwright.sync_api")


class DormantExecutor:
    def submit(self, *args, **kwargs):
        return None


@pytest.fixture
def browser_app(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(web, "PLANS_DIR", tmp_path / "runs" / "plans")
    monkeypatch.setattr(web, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(web, "_executor", DormantExecutor())
    monkeypatch.setattr(web, "_submission_slots", threading.BoundedSemaphore(50))
    # Browser tests exercise the real planner, not an interactive Google login.
    monkeypatch.setattr(web._local_auth, "is_verified", lambda project, source="auto": bool(project))
    monkeypatch.setattr(web._local_auth, "status", lambda: {"state": "done", "action": "verify"})
    monkeypatch.setattr(web, "pick_directory", lambda initial: None)
    web.RUNS_DIR.mkdir()
    web.PLANS_DIR.mkdir()
    web.OUTPUT_ROOT.mkdir()
    web._tasks.clear()
    web._processes.clear()

    server = make_server("127.0.0.1", 0, web.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(**({'executable_path': str(CHROME)} if CHROME.exists() else {}), headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.route("https://**", lambda route: route.abort())
        page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="domcontentloaded")
        page.fill("#project", "gee-test")
        page.dispatch_event("#project", "input")
        page.wait_for_function("authReady")
        yield page, tmp_path
        context.close()
        browser.close()
    server.shutdown()
    thread.join(5)
    web._tasks.clear()
    web._processes.clear()


def _accept_next_dialog(page):
    page.once("dialog", lambda dialog: dialog.accept())


def _fill_project(page, value):
    settings = page.locator('#connection-settings')
    if not settings.evaluate('el => el.open'):
        settings.locator(':scope > summary').click()
    page.fill('#project', value)


def test_local_connection_guides_next_action_without_automatic_login(browser_app, monkeypatch):
    page, _ = browser_app
    state = {'state': 'idle'}
    calls = []
    options = []
    monkeypatch.setattr(web._local_auth, 'status', lambda: dict(state))
    monkeypatch.setattr(web._local_auth, 'is_verified',
                        lambda project, source='auto': state.get('verified', False) and project == 'gee-test')

    def start(action, **kwargs):
        calls.append(action)
        options.append(kwargs)
        state.clear()
        state.update(state='running', action=action)
        return dict(state)

    monkeypatch.setattr(web._local_auth, 'start', start)
    page.evaluate('refreshLocalAuth()')
    button = page.locator('#local-auth-next')
    playwright.expect(button).to_have_text('检查本机凭据')
    assert calls == []
    button.click()
    playwright.expect(page.locator('#local-auth-cancel')).to_be_visible()
    assert calls == ['status']

    state.update(state='done', candidates=[{'source': 'earthengine', 'available': False}])
    page.evaluate('refreshLocalAuth()')
    playwright.expect(button).to_have_text('前往 Google 授权')
    assert calls == ['status']  # Finding no credential must not open Google.
    with page.expect_response('**/api/local-auth/start'):
        button.click()
    assert calls == ['status', 'login']
    assert options[-1]['force'] is False
    authorization_url = 'https://accounts.google.com/o/oauth2/auth?client_id=test'
    state.update(authorization_url=authorization_url)
    page.evaluate('refreshLocalAuth()')
    link = page.locator('#local-auth-link')
    playwright.expect(link).to_be_visible()
    assert link.get_attribute('href') == authorization_url
    page.route('**/api/local-auth/status', lambda route: route.fulfill(status=503))
    page.evaluate('refreshLocalAuth().catch(showLocalAuthError)')
    playwright.expect(link).to_be_hidden()
    playwright.expect(button).to_have_text('重新检查连接')
    page.unroute('**/api/local-auth/status')
    button.click()
    playwright.expect(link).to_be_visible()
    assert calls == ['status', 'login']  # Recover polling without starting another login.
    state.update(state='done')
    _fill_project(page, '')
    page.evaluate('refreshLocalAuth()')
    playwright.expect(button).to_be_disabled()
    playwright.expect(link).to_be_hidden()
    assert link.get_attribute('href') is None
    assert not page.evaluate('authReady')
    _fill_project(page, 'gee-test')
    page.evaluate('refreshLocalAuth()')
    playwright.expect(button).to_have_text('验证连接')
    with page.expect_response('**/api/local-auth/start'):
        button.click()
    state.update(state='error', error='Network timeout; retry verification')
    page.evaluate('refreshLocalAuth()')
    playwright.expect(button).to_have_text('验证连接')
    assert not page.evaluate('authReady')
    state.update(state='done', verified=True, error='')
    page.evaluate('refreshLocalAuth()')
    playwright.expect(button).to_be_hidden()
    assert not page.locator('#connection-settings').evaluate('el => el.open')
    _fill_project(page, 'changed-project')
    page.wait_for_function('!authReady')
    playwright.expect(button).to_be_visible()
    assert page.locator('#simple-btn-submit').is_disabled()
    page.locator('#local-auth-settings > summary').click()
    with page.expect_response('**/api/local-auth/start'):
        page.get_by_role('button', name='重新授权 / 更换账号', exact=True).click()
    assert calls[-1] == 'login' and options[-1]['force'] is True


def test_tessera_browser_plan_and_submit(browser_app):
    page, tmp_path = browser_app
    aoi = tmp_path / "aoi.wkt"
    aoi.write_text("POLYGON ((117.01 31.01, 117.09 31.01, 117.09 31.09, 117.01 31.09, 117.01 31.01))", encoding="utf-8")
    page.click("#simple-wf-grid")
    page.click("#scheme-tessera")
    page.set_input_files("#aoi-files", str(aoi))
    page.wait_for_function("document.querySelector('#simple-vector-file-name').textContent.includes('检查通过')")
    _accept_next_dialog(page)
    page.click("#simple-btn-submit")
    page.wait_for_selector(".task-status.queued")
    assert "grid_117.05_31.05" in page.locator("#simple-plan-summary").inner_text()
    assert "EPSG:32650" in page.locator("#simple-crs-summary").inner_text()
    assert "1 grids" in page.locator("#grid-legend").inner_text()


def test_mgrs_browser_shows_authoritative_plan_without_start(browser_app):
    page, tmp_path = browser_app
    aoi = tmp_path / "anhui.wkt"
    aoi.write_text("POLYGON ((117.01 31.01, 117.09 31.01, 117.09 31.09, 117.01 31.09, 117.01 31.01))", encoding="utf-8")
    page.click("#simple-wf-grid")
    page.set_input_files("#aoi-files", str(aoi))
    page.wait_for_function("document.querySelector('#simple-vector-file-name').textContent.includes('检查通过')")
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.click("#simple-btn-submit")
    page.wait_for_function("document.querySelector('#simple-plan-summary').textContent.includes('50RMV')")
    page.locator('#simple-plan-details > summary').click()
    text = page.locator("#simple-plan-summary").inner_text()
    assert "50RMV" in text and "50RNV" in text
    assert "EPSG:32650" in page.locator("#simple-crs-summary").inner_text()
    assert page.locator(".task-card").count() == 0


def test_point_shapefile_is_checked_and_submitted(browser_app):
    page, tmp_path = browser_app
    import geopandas as gpd
    from shapely.geometry import Point

    source = tmp_path / "points.shp"
    gpd.GeoDataFrame({"sample_id": ["p1"]}, geometry=[Point(117.05, 31.05)], crs="EPSG:4326").to_file(source)
    components = [str(path) for path in tmp_path.glob("points.*")]
    page.click("#point-source-shp")
    page.set_input_files("#simple-point-files", components)
    _accept_next_dialog(page)
    page.click("#simple-btn-submit")
    page.wait_for_selector(".task-status.queued")
    assert "Point" in page.locator("#simple-plan-summary").inner_text()
    assert "EPSG:4326" in page.locator("#simple-crs-summary").inner_text()


def test_point_browser_two_stage_preflight_and_submit(browser_app):
    page, tmp_path = browser_app
    samples = tmp_path / "points.csv"
    samples.write_text("sample_id,lon,lat\np1,117.05,31.05\n", encoding="utf-8")
    page.set_input_files("#simple-point-files", str(samples))
    _accept_next_dialog(page)
    page.click("#simple-btn-submit")
    page.wait_for_selector(".task-status.queued")
    assert "点位：1" in page.locator("#simple-plan-summary").inner_text()
    assert "EPSG:4326" in page.locator("#simple-crs-summary").inner_text()
    card_text = page.locator(".task-card").first.inner_text()
    assert "目标：点位 AEF 特征" in card_text
    assert "范围：points.csv · 1 点" in card_text
    assert "年份：2025" in card_text


def test_task_dock_is_horizontal_newest_first_and_scrollable(browser_app):
    page, tmp_path = browser_app
    for index in range(4):
        rid = f"task-{index}"
        web._tasks[rid] = {
            "status": "done",
            "progress": 1.0,
            "started_at": f"2026-08-13T12:00:0{index}",
            "finished_at": f"2026-08-13T12:01:0{index}",
            "params": {
                "workflow": "grid",
                "grid_scheme": "mgrs",
                "grid_ids": [f"50RM{index}"],
                "years": [2024, 2025],
            },
            "plan": {"grid_crs": ["EPSG:32650"], "raw_gib": 1.0},
            "output_dir": tmp_path / "a-very-long-research-output-directory" / f"output-{index}",
            "validation": {"valid": True, "errors": [], "warnings": []},
            "progress_detail": {
                "grid_id": f"50RM{index}",
                "eta_seconds": 120.0,
            },
        }
    page.evaluate("loadTasks()")
    page.wait_for_function("document.querySelectorAll('.task-card').length === 4")
    titles = page.locator(".task-card-title").all_inner_texts()
    assert "task-3" in titles[0]
    sidebar = page.locator("#sidebar").bounding_box()
    dock = page.locator("#task-panel").bounding_box()
    assert dock["x"] >= sidebar["width"]
    assert page.locator("#task-panel-body").evaluate("el => el.scrollWidth > el.clientWidth")
    first_card = page.locator(".task-card").first
    assert first_card.locator(".task-validation").inner_text() == "产物验证：通过"
    assert "当前格网 ETA 2.0 min" in first_card.inner_text()
    assert first_card.evaluate("el => el.scrollHeight <= el.clientHeight")
    assert first_card.locator(".task-card-actions").bounding_box()["y"] < first_card.bounding_box()["y"] + first_card.bounding_box()["height"]
    assert "嵌入式特征数据下载工具" in page.locator(".header-tag").inner_text()


def test_styled_file_pickers_and_server_output_browser(browser_app):
    page, _ = browser_app
    assert page.locator("#simple-point-files").is_hidden()
    assert page.locator("#point-file-picker").is_visible()
    assert page.locator("#checkpoint-interval").input_value() == "8"

    page.click("#simple-wf-grid")
    assert page.locator("#aoi-shapefile-picker .file-picker-label").inner_text() == "Shapefile"
    draw = page.locator("#btn-draw-simple")
    assert draw.locator(".file-picker-icon svg").get_attribute("aria-hidden") == "true"
    assert draw.locator(".file-picker-icon use").get_attribute("href") == "#icon-pencil"
    assert draw.locator(".file-picker-label").inner_text() == "矢量绘制"
    assert draw.locator(".file-picker-label").evaluate("el => el.getBoundingClientRect().height < 24")

    page.click("#output-location-options > summary")
    page.click("#output-browser-fallback")
    page.wait_for_selector("#directory-modal.visible")
    page.fill("#directory-new-name", "browser_selected")
    page.click("#directory-modal .directory-create button")
    page.wait_for_function("document.querySelector('#directory-current').textContent.includes('browser_selected')")
    page.click("#directory-modal .directory-actions .primary")
    assert page.locator("#simple-output").input_value() == "browser_selected"
    assert "browser_selected" in page.locator("#output-picker-detail").inner_text()


def test_portal_preview_export_invalidation_and_confirm(browser_app):
    import json

    page, tmp_path = browser_app
    page.click('#view-switch')
    page.wait_for_url('**/portal')
    page.wait_for_function("typeof webAuthMode !== 'undefined' && webAuthMode === 'local'")
    _fill_project(page, 'gee-test')
    samples = tmp_path / 'portal.csv'
    samples.write_text('sample_id,lon,lat\np1,117.05,31.05\n', encoding='utf-8')
    page.set_input_files('#simple-point-files', str(samples))
    page.click('#portal-preview')
    page.wait_for_function("!document.querySelector('#portal-download').disabled")
    assert len(web._tasks) == 0  # planning must not start a download
    assert 'portal.csv' in page.locator('#portal-plan-rows').inner_text()
    assert 'EPSG:4326' in page.locator('#portal-plan-rows').inner_text()
    with page.expect_download() as exported:
        page.click('#portal-export')
    content = json.loads(Path(exported.value.path()).read_text(encoding='utf-8'))
    assert content['product'] == 'AEF' and content['rows'] == 1
    assert 'plan_id' not in content and 'project' not in content
    _fill_project(page, 'gee-test-changed')
    assert page.locator('#portal-download').is_disabled()
    assert '条件已改变' in page.locator('#portal-plan-state').inner_text()
    page.click('#portal-preview')
    page.wait_for_function("!document.querySelector('#portal-download').disabled")
    _accept_next_dialog(page)
    page.click('#portal-download')
    page.wait_for_selector('#portal-task-rows .task-status.queued')
    assert len(web._tasks) == 1
    assert page.locator('#portal-download').is_disabled()
    assert page.locator('#portal-task-page').is_visible()
    assert 'portal.csv · 1 点' in page.locator('#portal-task-rows').inner_text()
    page.select_option('#portal-task-filter', 'done')
    assert '该状态下没有任务' in page.locator('#portal-task-rows').inner_text()


def test_portal_grid_plan_and_product_selection(browser_app):
    page, tmp_path = browser_app
    page.click('#view-switch')
    page.wait_for_function("typeof webAuthMode !== 'undefined' && webAuthMode === 'local'")
    _fill_project(page, 'gee-test')
    page.wait_for_function('authReady')
    aoi = tmp_path / 'portal.wkt'
    aoi.write_text('POLYGON ((117.01 31.01, 117.09 31.01, 117.09 31.09, 117.01 31.09, 117.01 31.01))', encoding='utf-8')
    page.click('#simple-wf-grid')
    page.click('#scheme-tessera')
    page.set_input_files('#aoi-files', str(aoi))
    page.wait_for_function("document.querySelector('#simple-vector-file-name').textContent.includes('检查通过')")
    page.click('#portal-preview')
    page.wait_for_function("!document.querySelector('#portal-download').disabled")
    assert 'grid_117.05_31.05' in page.locator('#portal-plan-rows').inner_text()
    assert 'EPSG:32650' in page.locator('#portal-plan-rows').inner_text()
    assert 'AEF' in page.locator('#portal-plan-rows').inner_text()
    page.click('.portal-products summary')
    assert page.locator('.portal-product-menu input[disabled]').count() == 1
    page.uncheck('#portal-aef')
    assert page.locator('#portal-preview').is_disabled()
    assert page.locator('#portal-download').is_disabled()
    assert page.locator('#preparation-progress').get_attribute('value') == '75'
    assert 'AEF与提取方式' in page.locator('#preparation-hint').inner_text()
    assert not web._tasks


def test_portal_task_rate_filter_and_mobile_layout(browser_app):
    page, tmp_path = browser_app
    web._tasks['demo-running'] = {
        'status': 'running', 'progress': .43,
        'log_path': tmp_path / 'not-yet-written.log',
        'params': {'workflow': 'grid', 'grid_scheme': 'tessera_0p1',
                   'grid_ids': ['grid_117.05_31.05'], 'years': [2025]},
        'output_dir': tmp_path / 'output',
        'progress_detail': {'rate_per_second': .25, 'eta_seconds': 240},
    }
    page.click('#view-switch')
    page.click('#portal-task-tab')
    page.wait_for_selector('#portal-task-rows .task-status.running')
    text = page.locator('#portal-task-rows').inner_text()
    assert '43%' in text and '0.25 块/s' in text and '4.0 min' in text
    page.select_option('#portal-task-filter', 'done')
    assert '该状态下没有任务' in page.locator('#portal-task-rows').inner_text()
    page.select_option('#portal-task-filter', 'active')
    assert 'demo-running' in page.locator('#portal-task-rows').inner_text()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.body.scrollWidth <= innerWidth')


def test_preparation_progress_and_real_preflight_activity(browser_app):
    page, tmp_path = browser_app
    page.wait_for_function('authReady')
    _fill_project(page, '')
    page.click('#simple-year-tags .year-tag.active')
    assert page.locator('#preparation-progress').get_attribute('value') == '25'
    assert '请填写Earth Engine Project' in page.locator('#preparation-hint').inner_text()
    assert page.locator('#simple-btn-submit').is_disabled()

    samples = tmp_path / 'readiness.csv'
    samples.write_text('sample_id,lon,lat\np1,117.05,31.05\n', encoding='utf-8')
    page.set_input_files('#simple-point-files', str(samples))
    assert page.locator('#preparation-progress').get_attribute('value') == '50'
    _fill_project(page, 'gee-test')
    page.wait_for_function('authReady')
    assert page.locator('#preparation-progress').get_attribute('value') == '75'
    page.click('#simple-year-tags [data-year="2025"]')
    assert page.locator('#preparation-progress').get_attribute('value') == '100'
    assert page.locator('#simple-btn-submit').is_enabled()

    observed = []

    def inspect_preflight(route):
        observed.append(page.locator('#preflight-progress').is_visible())
        assert page.locator('#preflight-progress').get_attribute('value') is None
        assert page.locator('#simple-btn-submit').is_disabled()
        assert page.locator('#submission-status').inner_text() == '正在预检…'
        route.continue_()  # preserve the browser's multipart file upload to the real planner

    page.route('**/api/plan/points', inspect_preflight)
    page.once('dialog', lambda dialog: dialog.dismiss())
    page.click('#simple-btn-submit')
    page.wait_for_function('!submittingTask')
    assert observed == [True]
    assert page.locator('#submission-status').is_hidden()
    assert page.locator('#preflight-progress').is_hidden()
    assert page.locator('#simple-btn-submit').is_enabled()
    assert not web._tasks


def test_auth_status_failure_explained_and_retryable(browser_app):
    page, tmp_path = browser_app
    samples = tmp_path / 'auth-retry.csv'
    samples.write_text('sample_id,lon,lat\np1,117.05,31.05\n', encoding='utf-8')
    page.set_input_files('#simple-point-files', str(samples))
    page.wait_for_function('authReady')
    page.route('**/api/auth/status', lambda route: route.fulfill(status=503, json={'error': 'Unavailable'}))
    page.evaluate('loadAuthStatus()')
    assert page.locator('#preparation-progress').get_attribute('value') == '75'
    assert '无法检查认证状态' in page.locator('#preparation-hint').inner_text()
    assert page.locator('#simple-btn-submit').is_disabled()
    assert page.locator('#auth-recheck').is_visible()
    page.unroute('**/api/auth/status')
    page.click('#auth-recheck')
    page.wait_for_function('authReady')
    assert page.locator('#preparation-progress').get_attribute('value') == '100'
    assert page.locator('#simple-btn-submit').is_enabled()


@pytest.mark.parametrize('portal', [False, True])
def test_preparation_card_layout_and_actionable_hint(browser_app, portal):
    page, _ = browser_app
    if portal:
        page.click('#view-switch')
        page.wait_for_function("typeof webAuthMode !== 'undefined' && webAuthMode === 'local'")
        _fill_project(page, 'gee-test')
    page.wait_for_function('typeof authReady !== "undefined" && authReady')
    _fill_project(page, '')
    assert page.locator('#preparation-percent').inner_text() == '50%'
    assert page.locator('#preparation-count').inner_text() == '已就绪 2 / 4 项'
    assert page.locator('#preparation-hint').inner_text() == '请填写Earth Engine Project；请选择点位文件。'
    assert page.locator('#preparation-checks > span.ready').count() == 2
    for width in (1600, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        boxes = page.locator('#preparation-checks > span').evaluate_all(
            'els => els.map(el => ({y:el.getBoundingClientRect().y, fits:el.scrollWidth <= el.clientWidth}))'
        )
        assert len({box['y'] for box in boxes}) == 1
        assert all(box['fits'] for box in boxes)
        assert page.locator('.preparation-status').evaluate('el => el.scrollWidth <= el.clientWidth')


def test_map_points_use_existing_csv_preflight_and_submission(browser_app):
    page, _ = browser_app
    assert '数据源与连接' in page.locator('#sidebar-scroll .step-title').first.inner_text()
    page.locator('#connection-settings > summary').click()
    assert '凭据不上传官网' in page.locator('#auth-help').inner_text()
    page.click('#point-source-map')
    page.evaluate('map.setView([31.05,117.05], 10)')
    page.locator('#map').click(position={'x': 350, 'y': 200})
    page.locator('#map').click(position={'x': 450, 'y': 240})
    assert '地图已选 2 个点' in page.locator('#point-file-status').inner_text()
    page.locator('#map path.leaflet-interactive').first.click()
    assert '地图已选 1 个点' in page.locator('#point-file-status').inner_text()
    csv = page.evaluate("document.querySelector('#simple-point-files').files[0].text()")
    assert csv.startswith('sample_id,lon,lat\nmap_1,')
    _accept_next_dialog(page)
    with page.expect_response(lambda response: response.url.endswith('/api/tasks')
                              and response.request.method == 'POST') as submitted:
        page.click('#simple-btn-submit')
    assert submitted.value.status == 202, submitted.value.json()
    page.wait_for_selector('.task-status.queued')
    task = next(iter(web._tasks.values()))
    assert task['params']['sample_count'] == 1
    assert task['plan']['source_name'] == 'map_points.csv'


def test_map_grid_selection_controls_signed_download_plan(browser_app):
    page, tmp_path = browser_app
    page.click('#view-switch')
    page.wait_for_function("typeof webAuthMode !== 'undefined' && webAuthMode === 'local'")
    _fill_project(page, 'gee-test')
    page.wait_for_function('authReady')
    page.click('#simple-wf-grid')
    page.click('#scheme-tessera')
    aoi = tmp_path / 'two_tiles.wkt'
    aoi.write_text('POLYGON ((117.01 31.01, 117.19 31.01, 117.19 31.09, 117.01 31.09, 117.01 31.01))', encoding='utf-8')
    page.set_input_files('#aoi-files', str(aoi))
    page.wait_for_function("document.querySelector('#simple-vector-file-name').textContent.includes('检查通过')")
    page.click('#preview-grid-selection')
    page.wait_for_function("!document.querySelector('#portal-download').disabled")
    assert page.locator('#portal-plan-rows tr').count() == 2
    position = page.evaluate('map.latLngToContainerPoint([31.05,117.05])')
    page.locator('#map').click(position=position)
    assert '已选择 1 个格网' in page.locator('#grid-selection-hint').inner_text()
    assert page.locator('#portal-download').is_disabled()
    page.click('#portal-preview')
    page.wait_for_function("!document.querySelector('#portal-download').disabled")
    assert page.locator('#portal-plan-rows tr').count() == 1
    assert 'grid_117.15_31.05' in page.locator('#portal-plan-rows').inner_text()
    _accept_next_dialog(page)
    page.click('#portal-download')
    page.wait_for_selector('#portal-task-rows .task-status.queued')
    task = next(iter(web._tasks.values()))
    assert task['params']['grid_ids'] == ['grid_117.15_31.05']


@pytest.mark.parametrize('output_format,label,flag', [
    ('zarr+geotiff', 'Zarr + GeoTIFF', '--export-geotiff'),
    ('geotiff', 'GeoTIFF（仅 TIFF）', '--geotiff-only'),
])
def test_geotiff_choice_and_public_source_disabled_in_gui(browser_app, output_format, label, flag):
    page, tmp_path = browser_app
    page.click('#view-switch')
    page.wait_for_function("typeof webAuthMode !== 'undefined' && webAuthMode === 'local'")
    _fill_project(page, 'gee-test')
    page.click('#simple-wf-grid')
    page.click('#scheme-tessera')
    page.select_option('#output-format', output_format)
    aoi = tmp_path / 'format.wkt'
    aoi.write_text('POLYGON ((117.01 31.01, 117.09 31.01, 117.09 31.09, 117.01 31.09, 117.01 31.01))', encoding='utf-8')
    page.set_input_files('#aoi-files', str(aoi))
    page.wait_for_function("document.querySelector('#simple-vector-file-name').textContent.includes('检查通过')")
    page.click('#portal-preview')
    page.wait_for_function("!document.querySelector('#portal-download').disabled")
    assert label in page.locator('#portal-plan-rows').inner_text()
    _accept_next_dialog(page)
    page.click('#portal-download')
    page.wait_for_selector('#portal-task-rows .task-status.queued')
    task = next(iter(web._tasks.values()))
    assert flag in task['command']
    page.locator('#source-experiments > summary').click()
    page.select_option('#data-source', 'public')
    assert page.locator('#portal-preview').is_disabled()
    assert page.locator('#public-source-controls').is_visible()
    assert '尚未' in page.locator('#auth-state').inner_text()


def test_native_folder_plan_change_cancel_and_submit(browser_app, monkeypatch):
    page, tmp_path = browser_app
    chosen = tmp_path / 'native 中文 folder'
    chosen.mkdir()
    second = tmp_path / 'another disk'
    second.mkdir()
    monkeypatch.setattr(web, 'pick_directory', lambda initial: str(chosen))
    page.click('#view-switch')
    page.wait_for_function("typeof webAuthMode !== 'undefined' && webAuthMode === 'local'")
    _fill_project(page, 'gee-test')
    page.wait_for_function('authReady')
    samples = tmp_path / 'native.csv'
    samples.write_text('sample_id,lon,lat\np1,117.05,31.05\n', encoding='utf-8')
    page.set_input_files('#simple-point-files', str(samples))
    page.click('#output-directory-picker')
    page.wait_for_function("document.querySelector('#native-output-token').value !== ''")
    assert str(chosen) in page.locator('#output-root-hint').inner_text()
    assert page.locator('#directory-modal').is_hidden()
    page.click('#portal-preview')
    page.wait_for_function("!document.querySelector('#portal-download').disabled")
    original_token = page.locator('#native-output-token').input_value()
    monkeypatch.setattr(web, 'pick_directory', lambda initial: None)
    with page.expect_response('**/api/output-directories/pick'):
        page.click('#output-directory-picker')
    playwright.expect(page.locator('#output-directory-picker')).to_be_enabled()
    playwright.expect(page.locator('#output-folder-status')).to_be_hidden()
    assert page.locator('#native-output-token').input_value() == original_token
    assert page.locator('#portal-download').is_enabled()
    monkeypatch.setattr(web, 'pick_directory', lambda initial: str(second))
    page.click('#output-directory-picker')
    page.wait_for_function("document.querySelector('#output-root-hint').textContent.includes('another disk')")
    assert page.locator('#portal-download').is_disabled()
    page.click('#portal-preview')
    page.wait_for_function("!document.querySelector('#portal-download').disabled")
    _accept_next_dialog(page)
    page.click('#portal-download')
    page.wait_for_selector('#portal-task-rows .task-status.queued')
    task = next(iter(web._tasks.values()))
    assert task['output_dir'].parent == second.resolve()
    assert task['plan']['output_root'] == str(second.resolve())


def test_native_folder_error_offers_web_fallback(browser_app, monkeypatch):
    page, _ = browser_app
    def no_desktop(initial):
        raise RuntimeError('Tk unavailable; use web folder browser')
    monkeypatch.setattr(web, 'pick_directory', no_desktop)
    page.click('#output-directory-picker')
    page.wait_for_function("document.querySelector('#output-folder-status').textContent.includes('无法打开')")
    assert page.locator('#native-output-token').input_value() == ''
    page.click('#output-browser-fallback')
    page.wait_for_selector('#directory-modal.visible')


def test_native_folder_old_server_explains_restart(browser_app):
    page, _ = browser_app
    page.route('**/api/output-directories/pick', lambda route: route.fulfill(
        status=404, content_type='text/html', body='<h1>Not Found</h1>'))
    page.click('#output-directory-picker')
    page.wait_for_function("document.querySelector('#output-folder-status').textContent.includes('重新运行 aef-grits-web')")
    assert page.locator('#native-output-token').input_value() == ''
    assert page.locator('#output-directory-picker').is_enabled()
    page.click('#output-browser-fallback')
    page.wait_for_selector('#directory-modal.visible')


def test_local_ui_and_static_website_visual_evidence(browser_app):
    page, _ = browser_app
    page.click('#view-switch')
    page.click('#simple-wf-grid')
    page.select_option('#output-format', 'geotiff')
    assert 'P0' in page.locator('#sidebar-scroll .step-title').first.inner_text()
    page.wait_for_selector('#map-loading', state='hidden')
    assert page.locator('#local-auth-cancel').is_hidden()
    assert page.locator('#local-auth-controls button').first.evaluate("""el =>
        getComputedStyle(el).borderRadius === getComputedStyle(document.documentElement).getPropertyValue('--radius-input').trim()
    """)
    assert page.locator('#output-root-hint').evaluate('el => el.scrollWidth <= el.clientWidth')
    evidence = os.environ.get('AEF_GRITS_ACCEPTANCE_DIR')
    if evidence:
        directory = Path(evidence)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / 'local-gui.png'), full_page=True)
        page.locator('#output-format').scroll_into_view_if_needed()
        page.screenshot(path=str(directory / 'local-gui-storage.png'), full_page=True)
    website = Path(__file__).resolve().parents[1] / 'website' / 'index.html'
    page.goto(website.as_uri())
    assert page.title() == 'AEF-GRiTS：下载 AEF 年度特征'
    assert page.locator('.hero h1').inner_text() == 'AEF数据\n端到端直传本地'
    assert page.get_by_role('link', name='首次使用，安装工具', exact=True).get_attribute('href') == '#install'
    assert page.get_by_role('link', name='已启动，打开下载页面', exact=True).get_attribute('href') == 'http://127.0.0.1:5555/portal'
    assert '默认保存到下载文件夹中的 AEF-GRiTS' in page.locator('.data-notes').text_content()
    assert 'python.exe -m webapp.launcher' in page.locator('#install').inner_text()
    for width in (1440, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.body.scrollWidth <= innerWidth')
        if evidence:
            page.screenshot(path=str(directory / f'website-{width}.png'), full_page=True)

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import expect

import webapp.app as web

from test_webapp_e2e import browser_app
from test_webapp_polish import polish_page


def test_storage_has_one_visible_location_and_quiet_cancel(polish_page):
    page, _, view = polish_page
    storage = page.locator('.step-storage')
    expect(storage.locator('button:visible')).to_have_count(1)
    expect(page.locator('#output-root-hint')).not_to_be_empty()
    original = page.locator('#output-root-hint').inner_text()
    with page.expect_response('**/api/output-directories/pick'):
        page.click('#output-directory-picker')
    expect(page.locator('#output-directory-picker')).to_be_enabled()
    expect(page.locator('#output-folder-status')).to_be_hidden()
    expect(page.locator('#output-root-hint')).to_have_text(original)
    expect(page.locator('#output-location-options')).not_to_have_attribute('open', '')
    if evidence := os.environ.get('AEF_GRITS_POLISH_EVIDENCE'):
        directory = Path(evidence)
        directory.mkdir(parents=True, exist_ok=True)
        storage.screenshot(path=str(directory / f'storage-{view}.png'))


def test_portal_plan_capacity_and_submission_do_not_repeat_in_sidebar(browser_app):
    page, tmp_path = browser_app
    page.click('#view-switch')
    page.wait_for_function("typeof webAuthMode !== 'undefined' && webAuthMode === 'local'")
    page.fill('#project', 'gee-test')
    page.wait_for_function('authReady')
    samples = tmp_path / 'four.csv'
    samples.write_text('sample_id,lon,lat\np1,117.05,31.05\np2,117.06,31.05\n'
                       'p3,117.05,31.06\np4,117.06,31.06\n', encoding='utf-8')
    page.set_input_files('#simple-point-files', str(samples))
    page.click('#portal-preview')
    expect(page.locator('#portal-download')).to_be_enabled()
    expect(page.locator('#portal-plan-capacity')).to_contain_text('1 KiB')
    expect(page.locator('#portal-plan-capacity')).to_contain_text('含预留')
    expect(page.locator('#portal-plan-rows')).to_contain_text('点位经纬度 EPSG:4326')
    expect(page.locator('#simple-plan-details')).to_be_hidden()
    expect(page.locator('#submission-status')).to_be_hidden()
    assert '0.000 GiB' not in page.locator('#portal-plan-page').inner_text()
    assert page.evaluate('formatBytes(null)') == '—'
    assert page.evaluate('formatBytes(0)') == '0 B'
    assert page.evaluate('formatBytes(1024)') == '1 KiB'
    assert page.evaluate('formatBytes(1024 ** 3)') == '1 GiB'
    directory = None
    if evidence := os.environ.get('AEF_GRITS_POLISH_EVIDENCE'):
        directory = Path(evidence)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / 'planning.png'))
    page.once('dialog', lambda dialog: dialog.accept())
    page.click('#portal-download')
    expect(page.locator('#portal-task-rows .task-status.queued')).to_be_visible()
    expect(page.locator('#portal-task-rows')).to_contain_text('1 KiB')
    expect(page.locator('#simple-plan-details')).to_be_hidden()
    expect(page.locator('#submission-status')).to_be_hidden()
    expect(page.locator('#portal-task-tab')).to_have_attribute('aria-selected', 'true')
    budget = page.locator('#portal-task-rows details')
    budget.locator('summary').click()
    budget.locator('summary').focus()
    page.evaluate('loadTasks()')
    expect(budget).to_have_attribute('open', '')
    expect(budget.locator('summary')).to_be_focused()
    budget.locator('summary').click()
    if directory:
        page.screenshot(path=str(directory / 'submitted.png'))


def test_missing_credentials_guides_explicit_google_authorization(browser_app):
    page, _ = browser_app
    page.route('**/api/local-auth/status', lambda route: route.fulfill(json={
        'state': 'done', 'verified': False,
        'candidates': [{'source': 'earthengine', 'available': False}],
    }))
    starts = []
    page.on('request', lambda request: starts.append(request.url)
            if request.url.endswith('/api/local-auth/start') else None)
    page.evaluate('refreshLocalAuth()')
    expect(page.locator('#local-auth-result')).to_contain_text('未找到本机凭据')
    expect(page.locator('#simple-btn-submit')).to_be_disabled()
    assert starts == []  # Discovery must not launch Google consent automatically.


def test_task_capacity_survives_progress_refresh(browser_app):
    page, tmp_path = browser_app
    web._tasks['capacity-progress'] = {
        'status': 'done', 'progress': 1,
        'params': {'workflow': 'points', 'years': [2025]},
        'plan': {'raw_bytes': 1024}, 'output_dir': tmp_path / 'output',
        'validation': {'valid': True},
    }
    page.evaluate('loadTasks()')
    text = page.locator('#progtext-capacity-progress')
    expect(text).to_contain_text('预计 1 KiB')
    page.evaluate("updateTaskProgress('capacity-progress', 0.5)")
    expect(text).to_contain_text('进度 50%')
    expect(text).to_contain_text('预计 1 KiB')
    expect(text).to_contain_text('产物验证：通过')

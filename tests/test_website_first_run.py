from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import threading

import pytest

playwright = pytest.importorskip('playwright.sync_api')
expect = playwright.expect
WEBSITE = Path(__file__).resolve().parents[1] / 'website'


@pytest.fixture
def website_browser():
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(WEBSITE)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as manager:
            chrome = Path(r'C:\Program Files\Google\Chrome\Application\chrome.exe')
            browser = manager.chromium.launch(**({'executable_path': str(chrome)} if chrome.exists() else {}), headless=True)
            try:
                yield browser, f'http://127.0.0.1:{server.server_port}'
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_first_run_commands_copy_without_executing_or_probing_localhost(website_browser):
    browser, url = website_browser
    context = browser.new_context(permissions=['clipboard-read', 'clipboard-write'], viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    page.goto(url + '/#install')
    expect(page.locator('#release-note')).to_contain_text('安装包尚未发布')
    assert page.locator('#install-windows').inner_text() == (
        'py -3.12 -m venv .venv\n.\\.venv\\Scripts\\python.exe -m pip install ".[download,web]"'
    )
    for command_id in ('install-windows', 'launch-windows', 'install-unix', 'launch-unix'):
        if command_id == 'install-unix':
            page.get_by_text('macOS / Linux / Terminal', exact=True).click()
        page.locator(f'[data-copy="{command_id}"]').click()
        expect(page.locator('#copy-status')).to_contain_text('已复制')
        # Windows clipboard text uses CRLF; the shell command is otherwise unchanged.
        page.wait_for_function(
            "async expected => (await navigator.clipboard.readText()).replace(/\\r\\n/g, '\\n') === expected",
            arg=page.locator(f'#{command_id}').inner_text(),
        )
    local_link = page.locator('#open-workbench')
    assert local_link.get_attribute('href') == 'http://127.0.0.1:5555/portal'
    assert local_link.get_attribute('target') == '_blank'
    assert 'noopener' in local_link.get_attribute('rel')
    assert all(request.startswith(url + '/') for request in requests)
    context.close()


def test_copy_permission_failure_selects_the_command(website_browser):
    browser, url = website_browser
    context = browser.new_context()
    context.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async () => {throw new Error('denied');}}});")
    page = context.new_page()
    page.goto(url + '/#install')
    page.locator('[data-copy="launch-windows"]').click()
    expect(page.locator('#copy-status')).to_contain_text('已选中命令')
    assert page.evaluate('window.getSelection().toString()') == page.locator('#launch-windows').inner_text()
    context.close()


@pytest.mark.parametrize(('device', 'user_agent', 'width', 'height'), [
    ('iphone', 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1', 390, 844),
    ('android', 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0.0.0 Mobile Safari/537.36', 360, 800),
    ('iphone-landscape', 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1', 844, 390),
])
def test_phone_shows_desktop_notice_and_copies_url(website_browser, device, user_agent, width, height):
    browser, url = website_browser
    context = browser.new_context(
        user_agent=user_agent, is_mobile=True, has_touch=True,
        viewport={'width': width, 'height': height},
        permissions=['clipboard-read', 'clipboard-write'],
    )
    page = context.new_page()
    requests = []
    page.on('request', lambda request: requests.append(request.url))
    address = url + '/?from=phone#install'
    page.goto(address)
    expect(page.get_by_role('heading', name='请在电脑上打开')).to_be_visible()
    for selector in ('.desktop-home', '.nav-links', '.footer'):
        expect(page.locator(selector)).to_be_hidden()
    expect(page.get_by_role('link', name='by Kae')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.get_by_role('button', name='复制网址', exact=True).click()
    expect(page.locator('#page-copy-status')).to_have_text('网址已复制，请在电脑上打开。')
    page.wait_for_function('async expected => await navigator.clipboard.readText() === expected', arg=address)
    assert all(request.startswith(url + '/') for request in requests)
    if evidence := os.environ.get('AEF_GRITS_ACCEPTANCE_DIR'):
        directory = Path(evidence)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f'notice-{device}.png'), full_page=True)
    context.close()


def test_phone_copy_failure_keeps_url_and_feedback_visible(website_browser):
    browser, url = website_browser
    context = browser.new_context(user_agent='Mozilla/5.0 (iPhone) Mobile', viewport={'width': 390, 'height': 844})
    context.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async () => {throw new Error('denied');}}});")
    page = context.new_page()
    address = url + '/#install'
    page.goto(address)
    page.get_by_role('button', name='复制网址', exact=True).click()
    expect(page.locator('#page-copy-status')).to_be_visible()
    expect(page.locator('#page-copy-status')).to_have_text('请长按上方网址，选择复制。')
    expect(page.locator('#page-address')).to_have_text(address)
    assert page.evaluate('window.getSelection().toString()') == address
    context.close()


def test_first_run_is_responsive_and_usable_without_javascript(website_browser):
    browser, url = website_browser
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(url)
    expect(page.locator('#launch-windows')).to_be_visible()
    expect(page.locator('[data-copy="launch-windows"]')).to_be_hidden()
    context.close()
    context = browser.new_context()
    page = context.new_page()
    for width in (1440, 390):
        page.set_viewport_size({'width': width, 'height': 1000})
        page.goto(url + '/#main')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if evidence := os.environ.get('AEF_GRITS_ACCEPTANCE_DIR'):
            directory = Path(evidence)
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f'home-{width}.png'), full_page=True)
            page.locator('#install').evaluate("el => el.scrollIntoView({block: 'start', behavior: 'instant'})")
            page.screenshot(path=str(directory / f'first-use-{width}.png'))
    context.close()

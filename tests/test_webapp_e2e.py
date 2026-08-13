from __future__ import annotations

from pathlib import Path
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
    if not CHROME.exists():
        pytest.skip("System Chrome is unavailable")
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(web, "PLANS_DIR", tmp_path / "runs" / "plans")
    monkeypatch.setattr(web, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(web, "_executor", DormantExecutor())
    monkeypatch.setattr(web, "_submission_slots", threading.BoundedSemaphore(50))
    web.RUNS_DIR.mkdir()
    web.PLANS_DIR.mkdir()
    web.OUTPUT_ROOT.mkdir()
    web._tasks.clear()
    web._processes.clear()

    server = make_server("127.0.0.1", 0, web.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with playwright.sync_playwright() as manager:
        browser = manager.chromium.launch(executable_path=str(CHROME), headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.route("https://**", lambda route: route.abort())
        page.goto(f"http://127.0.0.1:{server.server_port}", wait_until="domcontentloaded")
        yield page, tmp_path
        context.close()
        browser.close()
    server.shutdown()
    thread.join(5)
    web._tasks.clear()
    web._processes.clear()


def _accept_next_dialog(page):
    page.once("dialog", lambda dialog: dialog.accept())


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
    assert draw.locator(".file-picker-icon").inner_text() == "✏️"
    assert draw.locator(".file-picker-label").inner_text() == "矢量绘制"
    assert draw.locator(".file-picker-label").evaluate("el => el.getBoundingClientRect().height < 24")

    page.click("#output-directory-picker")
    page.wait_for_selector("#directory-modal.visible")
    page.fill("#directory-new-name", "browser_selected")
    page.click("#directory-modal .directory-create button")
    page.wait_for_function("document.querySelector('#directory-current').textContent.includes('browser_selected')")
    page.click("#directory-modal .directory-actions .primary")
    assert page.locator("#simple-output").input_value() == "browser_selected"
    assert "browser_selected" in page.locator("#output-picker-detail").inner_text()

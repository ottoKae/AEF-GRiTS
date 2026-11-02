from __future__ import annotations

import os
from pathlib import Path
import re

import pytest

from test_webapp_e2e import browser_app
from playwright.sync_api import expect

import webapp.app as web


@pytest.fixture(params=["simple", "portal"])
def polish_page(browser_app, request):
    page, tmp_path = browser_app
    if request.param == "portal":
        page.click("#view-switch")
        page.wait_for_url("**/portal")
    page.wait_for_function("webAuthMode === 'local'")
    page.fill("#project", "gee-test")
    page.dispatch_event("#project", "input")
    page.wait_for_function("authReady")
    return page, tmp_path, request.param


@pytest.fixture
def task_view(polish_page):
    page, tmp_path, view = polish_page
    web._tasks["polish-task"] = {
        "status": "done",
        "progress": 1.0,
        "params": {"workflow": "grid", "grid_scheme": "mgrs",
                   "grid_ids": ["50RMV"], "years": [2025]},
        "output_dir": tmp_path / "output",
    }
    page.evaluate("loadTasks()")
    if view == "portal":
        page.click("#portal-task-tab")
    container = page.locator("#portal-task-rows" if view == "portal" else "#task-panel-body")
    expect(container.locator('[data-action="results"]')).to_be_visible()
    return page, container, view


def _check_modal_keyboard(page, modal):
    assert modal.evaluate("el => el instanceof HTMLDialogElement && el.open")
    title = modal.get_attribute("aria-labelledby")
    assert title and page.locator(f"#{title}").inner_text().strip()
    assert modal.locator(".modal-card-header button").get_attribute("aria-label")
    assert modal.evaluate("el => el.contains(document.activeElement)")
    controls = modal.locator("button:enabled, input:enabled, a[href], [tabindex='0']")
    for control, key in ((controls.last, "Tab"), (controls.first, "Shift+Tab")):
        control.focus()
        page.keyboard.press(key)
        if not modal.evaluate("el => el.contains(document.activeElement)"):
            # Native dialogs allow browser chrome, but never background document controls.
            assert page.evaluate("!document.hasFocus() && document.activeElement === document.body")
            page.keyboard.press(key)
        assert modal.evaluate("el => el.contains(document.activeElement)")
    page.locator("#project").focus()
    assert modal.evaluate("el => el.contains(document.activeElement)")


def _screenshot(page, name):
    if evidence := os.environ.get("AEF_GRITS_POLISH_EVIDENCE"):
        directory = Path(evidence)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"{name}.png"), full_page=True)


def _element_colors(elements):
    return elements.evaluate_all(r"""els => els.map(el => {
      const context = document.createElement('canvas').getContext('2d');
      const rgb = color => {
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = color;
        context.fillRect(0, 0, 1, 1);
        const values = context.getImageData(0, 0, 1, 1).data;
        return [values[0], values[1], values[2], values[3] / 255];
      };
      const layers = [];
      for (let node = el; node; node = node.parentElement) layers.push(rgb(getComputedStyle(node).backgroundColor));
      let background = [255, 255, 255];
      for (const layer of layers.reverse()) {
        const alpha = layer[3] ?? 1;
        background = background.map((value, index) => layer[index] * alpha + value * (1 - alpha));
      }
      return {text:el.textContent, foreground:rgb(getComputedStyle(el).color).slice(0, 3), background};
    })""")


def _luminance(color):
    channels = [value / 255 for value in color]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
    return sum(channel * weight for channel, weight in zip(linear, (.2126, .7152, .0722)))


def test_year_buttons_keyboard_and_select_all(polish_page):
    page, _, _ = polish_page
    years = page.locator("#simple-year-tags .year-tag")
    assert years.count() > 1
    assert years.evaluate_all("els => els.every(el => el.tagName === 'BUTTON' && el.type === 'button')")
    first = years.first
    expect(first).to_have_attribute("aria-pressed", "false")
    first.focus()
    first.press("Space")
    expect(first).to_have_attribute("aria-pressed", "true")
    first.press("Enter")
    expect(first).to_have_attribute("aria-pressed", "false")

    all_years = page.locator("#simple-year-all")
    all_years.focus()
    all_years.press("Space")
    expect(all_years).to_be_checked()
    assert years.evaluate_all("els => els.every(el => el.getAttribute('aria-pressed') === 'true')")
    first.focus()
    first.press("Space")
    expect(all_years).not_to_be_checked()
    first.press("Enter")
    expect(all_years).to_be_checked()
    all_years.focus()
    all_years.press("Space")
    assert years.evaluate_all("els => els.every(el => el.getAttribute('aria-pressed') === 'false')")
    assert page.evaluate("getSelectedYears()") == []


def test_year_select_all_shares_heading_row(polish_page):
    page, _, view = polish_page
    for width in (320, 390, 1050, 1600):
        page.set_viewport_size({"width": width, "height": 900})
        heading = page.locator(".step-time .step-title")
        label = heading.locator("#simple-year-all-wrap")
        expect(label).to_be_visible()
        assert heading.evaluate("el => el.scrollWidth <= el.clientWidth")
        caption_box = heading.locator(".step-caption").bounding_box()
        label_box = label.bounding_box()
        assert abs(caption_box["y"] + caption_box["height"] / 2
                   - label_box["y"] - label_box["height"] / 2) <= 1
        assert label_box["x"] >= caption_box["x"] + caption_box["width"]
    page.set_viewport_size({"width": 1600, "height": 1000})
    page.locator(".step-time").scroll_into_view_if_needed()
    _screenshot(page, f"year-row-{view}")


def test_basemap_buttons_keyboard_selection(polish_page):
    page, _, _ = polish_page
    toggle = page.locator("#toolmap")
    items = page.locator(".basemap-item")
    assert items.evaluate_all("els => els.every(el => el.tagName === 'BUTTON' && el.type === 'button')")
    for mode, key in (("sat", "Enter"), ("grid", "Space"), ("road", "Enter")):
        toggle.focus()
        toggle.press("Enter")
        expect(toggle).to_have_attribute("aria-expanded", "true")
        item = page.locator(f'.basemap-item[data-base="{mode}"]')
        item.focus()
        item.press(key)
        expect(item).to_have_attribute("aria-pressed", "true")
        expect(page.locator('.basemap-item[aria-pressed="true"]')).to_have_count(1)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        assert page.evaluate("baseLayerMode") == mode
    toggle.press("Enter")
    items.first.focus()
    page.keyboard.press("Escape")
    expect(toggle).to_be_focused()
    expect(toggle).to_have_attribute("aria-expanded", "false")


def test_directory_dialog_keyboard_focus_and_dismissal(polish_page):
    page, _, view = polish_page
    page.click('#output-location-options > summary')
    trigger = page.locator("#output-browser-fallback")
    modal = page.locator("#directory-modal")
    trigger.focus()
    trigger.press("Enter")
    expect(modal).to_be_visible()
    expect(page.locator("#directory-up")).to_be_disabled()
    _check_modal_keyboard(page, modal)
    assert modal.locator('label[for="directory-new-name"]').inner_text().strip()
    _screenshot(page, f"directory-{view}")
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()
    expect(modal).not_to_have_class(re.compile(r"\bvisible\b"))
    expect(trigger).to_be_focused()
    expect(modal).not_to_have_attribute("open", "")

    for dismiss in ("backdrop", "close"):
        trigger.press("Enter")
        expect(modal).to_be_visible()
        modal.locator("h3").click()
        expect(modal).to_be_visible()
        if dismiss == "backdrop":
            page.mouse.click(8, 8)
        else:
            modal.locator(".modal-card-header button").press("Enter")
        expect(modal).to_be_hidden()
        expect(modal).not_to_have_class(re.compile(r"\bvisible\b"))
        expect(trigger).to_be_focused()


def test_results_dialog_restores_refreshed_trigger(task_view):
    page, container, view = task_view
    page.route("**/api/tasks/polish-task/results", lambda route: route.fulfill(
        json={"run_id": "polish-task", "validation": {"valid": True}}))
    trigger = container.locator('[data-run="polish-task"][data-action="results"]')
    modal = page.locator("#result-modal")
    trigger.focus()
    trigger.press("Enter")
    expect(modal).to_be_visible()
    expect(page.locator("#result-content")).to_contain_text("polish-task")
    _check_modal_keyboard(page, modal)
    old_trigger = trigger.element_handle()
    page.evaluate("loadTasks()")
    assert not old_trigger.evaluate("el => el.isConnected")
    assert modal.evaluate("el => el.contains(document.activeElement)")
    _screenshot(page, f"results-{view}")
    page.keyboard.press("Escape")
    expect(modal).to_be_hidden()
    expect(modal).not_to_have_class(re.compile(r"\bvisible\b"))
    expect(trigger).to_be_focused()
    expect(modal).not_to_have_attribute("open", "")

    for dismiss in ("backdrop", "close"):
        trigger.press("Enter")
        expect(modal).to_be_visible()
        if dismiss == "backdrop":
            page.mouse.click(8, 8)
        else:
            modal.locator(".modal-card-header button").press("Enter")
        expect(modal).to_be_hidden()
        expect(modal).not_to_have_class(re.compile(r"\bvisible\b"))
        expect(trigger).to_be_focused()


def test_task_refresh_preserves_focus_and_handles_missing_task(task_view):
    page, container, _ = task_view
    action = container.locator('[data-run="polish-task"][data-action="path"]')
    action.focus()
    page.evaluate("loadTasks()")
    expect(action).to_be_focused()
    web._tasks["polish-task"]["progress"] = .75
    page.evaluate("loadTasks()")
    expect(action).to_be_focused()
    assert page.evaluate("tasks[0].progress") == .75
    page.locator("#project").focus()
    page.evaluate("loadTasks()")
    expect(page.locator("#project")).to_be_focused()
    action.focus()
    web._tasks.clear()
    page.evaluate("loadTasks()")
    expect(container).to_be_focused()
    expect(container.locator("[data-action]")).to_have_count(0)


def test_focus_ring_and_reduced_motion(task_view):
    page, _, view = task_view
    year = page.locator("#simple-year-tags .year-tag").first
    year.focus()
    year.press("Enter")
    assert year.evaluate("el => el.matches(':focus-visible')")
    ring = year.evaluate("el => ({width:parseFloat(getComputedStyle(el).outlineWidth), style:getComputedStyle(el).outlineStyle})")
    assert ring["width"] >= 2 and ring["style"] != "none"
    _screenshot(page, f"keyboard-focus-{view}")
    page.locator(".task-progress-bar").evaluate("el => el.classList.add('indeterminate')")
    page.emulate_media(reduced_motion="reduce")
    for selector in (".map-spinner", ".task-progress-bar.indeterminate"):
        style = page.locator(selector).evaluate("el => ({animation:getComputedStyle(el).animationName, transition:getComputedStyle(el).transitionDuration})")
        assert style["animation"] == "none"
        assert all(float(value.rstrip("s")) <= .01 for value in style["transition"].split(","))
    page.reload(wait_until="domcontentloaded")
    page.wait_for_function("mapReady")
    assert page.evaluate("['zoomAnimation', 'fadeAnimation', 'markerZoomAnimation', 'inertia'].every(key => map.options[key] === false)")
    state = page.evaluate("""() => {
      map.setView([35, 105], 6, {animate:false});
      flyChina();
      return {zoom:map.getZoom(), animating:Boolean(map._animatingZoom || map._panAnim?._inProgress)};
    }""")
    assert state == {"zoom": 4, "animating": False}


def test_step_badge_and_secondary_text_contrast_and_layout(polish_page):
    page, _, view = polish_page
    colors = _element_colors(page.locator(".step-number:visible, #output-root-hint"))
    assert len(colors) == 5
    for item in colors:
        luminance = [_luminance(item[key]) for key in ("foreground", "background")]
        ratio = (max(luminance) + .05) / (min(luminance) + .05)
        assert ratio >= 4.5, (item["text"], ratio)
    for width in (1600, 390):
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate("document.body.scrollWidth <= innerWidth")
        assert page.locator(".step-number").evaluate_all("els => els.every(el => el.scrollWidth <= el.clientWidth)")
        _screenshot(page, f"layout-{view}-{width}")


def test_light_workbench_sections_and_collapsible_help(polish_page):
    page, _, view = polish_page
    cards = page.locator("#sidebar-scroll > .step-card")
    expect(cards).to_have_count(4)
    expect(cards.locator(".step-number")).to_have_text(["P0", "P1", "P2", "P3"])
    expect(page.locator(".step-card .step-card")).to_have_count(0)
    expect(page.locator(".step-location > .location-options")).to_have_count(2)
    surfaces = _element_colors(cards)
    headers = _element_colors(cards.locator(".step-title"))
    for item in surfaces + headers:
        foreground, background = (_luminance(item[key]) for key in ("foreground", "background"))
        assert background >= .75
        assert (background + .05) / (foreground + .05) >= 4.5
    assert len({tuple(item["background"]) for item in headers}) > 1
    assert all(max(abs(a - b) for a, b in zip(card["background"], header["background"])) >= 5
               for card, header in zip(surfaces, headers))

    page.click("#simple-wf-grid")
    helps = page.locator(".step-card details.help-details")
    expect(helps).to_have_count(4)
    assert helps.evaluate_all("els => els.every(el => !el.open)")
    for details in helps.all():
        summary = details.locator(":scope > summary")
        content = details.locator("p, button").first
        assert summary.inner_text().strip() and content.text_content().strip()
        expect(content).to_be_hidden()
        summary.focus()
        summary.press("Enter")
        expect(details).to_have_attribute("open", "")
        expect(content).to_be_visible()
        expect(summary).to_be_focused()
        summary.press("Space")
        expect(content).to_be_hidden()
    expect(page.locator("#output-folder-status")).to_be_hidden()
    for selector in ("#auth-help", "#local-auth-result", "#output-root-hint", "#preparation-hint"):
        expect(page.locator(selector)).to_be_visible()
        assert page.locator(selector).evaluate("el => !el.closest('details')")
    for width in (1600, 390):
        page.set_viewport_size({"width": width, "height": 1000})
        for workflow in ("point", "grid"):
            page.click(f"#simple-wf-{workflow}")
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert cards.evaluate_all("els => els.every(el => el.scrollWidth <= el.clientWidth)")
            if width == 390:
                assert page.locator("#sidebar, #sidebar-scroll").evaluate_all("els => els.every(el => el.scrollHeight <= el.clientHeight)")
                sidebar = page.locator("#sidebar").bounding_box()
                assert page.locator("#workspace").bounding_box()["y"] >= sidebar["y"] + sidebar["height"]
            _screenshot(page, f"light-{view}-{workflow}-{width}")


def test_compact_header_and_mobile_product_menu(polish_page):
    page, _, view = polish_page
    header = page.locator("#header")
    expect(header.locator("h1")).to_have_text("AEF-GRiTS")
    for selector in ("#auth-state", "#view-switch", 'a[href="https://github.com/ottoKae/AEF-GRiTS"]'):
        expect(header.locator(selector)).to_be_visible()
    row = "#header > .header-brand, #header-product, #header > .header-session, #header > .header-actions"
    expect(page.locator(row)).to_have_count(4)
    if view == "portal":
        expect(header.locator("#header-product #portal-nav")).to_have_count(1)
    centers = page.locator(row).evaluate_all("els => els.map(el => {const r=el.getBoundingClientRect(); return r.y+r.height/2;})")
    assert max(centers) - min(centers) <= 6
    _screenshot(page, f"header-{view}-1600")
    page.set_viewport_size({"width": 390, "height": 1000})
    assert page.evaluate("document.body.scrollWidth <= innerWidth")
    expect(header.locator("#view-switch")).to_be_visible()
    _screenshot(page, f"header-{view}-390")
    if view == "portal":
        summary = header.locator(".portal-products summary")
        summary.focus()
        summary.press("Enter")
        menu = header.locator(".portal-product-menu")
        expect(menu).to_be_visible()
        expect(menu.locator("#portal-aef")).to_be_checked()
        bounds = menu.bounding_box()
        assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= 390
        assert page.evaluate("document.body.scrollWidth <= innerWidth")
        _screenshot(page, "header-menu-portal-390")


def test_task_panel_collapse_preserves_tasks_and_map_size(task_view):
    page, container, view = task_view
    prefix = "portal-panel" if view == "portal" else "task-panel"
    panel = page.locator(f"#{prefix}")
    body = page.locator(f"#{prefix}-body")
    toggle = page.locator(f"#{prefix}-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(toggle).to_have_attribute("aria-controls", f"{prefix}-body")
    map_height = page.locator("#map").evaluate("el => el.clientHeight")
    toggle.focus()
    toggle.press("Space")
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(panel).to_have_class(re.compile(r"\bis-collapsed\b"))
    expect(body).to_be_hidden()
    expect(toggle).to_be_visible()
    container.locator('[data-action="path"]').focus()
    expect(toggle).to_be_focused()
    page.wait_for_function("height => document.getElementById('map').clientHeight > height && map.getSize().y === document.getElementById('map').clientHeight", arg=map_height)
    web._tasks["polish-extra"] = dict(web._tasks["polish-task"])
    page.locator("#project").focus()
    page.evaluate("loadTasks()")
    expect(page.locator("#project")).to_be_focused()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(body).to_be_hidden()
    assert page.evaluate("tasks.length") == 2
    if view == "portal":
        expect(page.locator("#portal-task-count")).to_have_text("2")
        expect(page.locator("#portal-plan-tab")).to_be_visible()
        expect(page.locator("#portal-task-tab")).to_be_visible()
    _screenshot(page, f"panel-collapsed-{view}")
    toggle.press("Enter")
    expect(body).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(container.locator('[data-action="results"]')).to_have_count(2)
    page.wait_for_function("height => document.getElementById('map').clientHeight === height && map.getSize().y === height", arg=map_height)
    toggle.press("Enter")
    page.reload(wait_until="domcontentloaded")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(body).to_be_visible()


@pytest.mark.parametrize("polish_page", ["portal"], indirect=True)
def test_portal_collapsing_preserves_real_plan_and_filter(polish_page):
    page, tmp_path, _ = polish_page
    samples = tmp_path / "collapse-plan.csv"
    samples.write_text("sample_id,lon,lat\np1,117.05,31.05\n", encoding="utf-8")
    page.set_input_files("#simple-point-files", str(samples))
    page.click("#portal-preview")
    expect(page.locator("#portal-download")).to_be_enabled()
    plan = page.locator("#portal-plan-rows").inner_text()
    assert "collapse-plan.csv" in plan and "EPSG:4326" in plan
    assert not web._tasks
    page.click("#portal-task-tab")
    page.select_option("#portal-task-filter", "done")
    page.click("#portal-panel-toggle")
    page.evaluate("loadTasks()")
    expect(page.locator("#portal-panel-toggle")).to_have_attribute("aria-expanded", "false")
    page.click("#portal-plan-tab")
    expect(page.locator("#portal-panel-toggle")).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#portal-plan-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#portal-plan-page")).to_be_visible()
    assert page.locator("#portal-plan-rows").inner_text() == plan
    expect(page.locator("#portal-download")).to_be_enabled()
    expect(page.locator("#portal-export")).to_be_enabled()
    page.click("#portal-panel-toggle")
    page.click("#portal-task-tab")
    expect(page.locator("#portal-panel-toggle")).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#portal-task-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#portal-task-filter")).to_have_value("done")
    assert not web._tasks

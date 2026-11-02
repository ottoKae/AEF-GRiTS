from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_webapp_e2e import browser_app
from test_webapp_polish import _element_colors, _luminance, polish_page


def _capture_viewport(page, name):
    if evidence := os.environ.get("AEF_GRITS_POLISH_EVIDENCE"):
        directory = Path(evidence)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(directory / f"{name}.png"), full_page=False)


def _menu_hit(page, selector):
    return page.evaluate("""selector => {
      const menu = document.querySelector('.portal-product-menu');
      const a = menu.getBoundingClientRect();
      const b = document.querySelector(selector).getBoundingClientRect();
      const left = Math.max(a.left, b.left), right = Math.min(a.right, b.right);
      const top = Math.max(a.top, b.top), bottom = Math.min(a.bottom, b.bottom);
      const x = (left + right) / 2, y = (top + bottom) / 2;
      const hit = document.elementFromPoint(x, y);
      return {overlap:left < right && top < bottom, x, y,
        menuOnTop:menu.contains(hit), hit:hit?.outerHTML.slice(0, 180)};
    }""", selector)


def test_dose_header_type_and_surface_hierarchy(polish_page):
    page, _, _ = polish_page
    assert page.locator("#header h1").evaluate("el => getComputedStyle(el).fontSize") == "24px"
    assert page.locator("#header-product > strong, #portal-selected").evaluate("el => getComputedStyle(el).fontSize") == "18px"
    sizes = page.locator(".header-tag, .header-session-label, #auth-state, #view-switch, .header-actions a[href*='github.com']").evaluate_all(
        "els => els.map(el => getComputedStyle(el).fontSize)")
    assert sizes == ["16px"] * 5
    metadata = page.locator(".header-product-meta, #portal-nav p, .portal-products summary span")
    assert metadata.count() >= 1
    assert metadata.evaluate_all("els => els.every(el => getComputedStyle(el).fontSize === '16px')")
    background = _element_colors(page.locator("body"))[0]["background"]
    assert _luminance(background) >= .75
    assert background[0] > background[2] and background[1] >= background[2]
    header = page.locator("#header")
    assert header.bounding_box()["height"] <= 88
    header_background = _element_colors(header)[0]["background"]
    assert all(group["background"] == header_background
               for group in _element_colors(page.locator("#header > div")))
    sections = _element_colors(page.locator(".step-title"))
    assert len(sections) == 4
    assert len({tuple(section["background"]) for section in sections}) > 1
    for section in sections:
        light, dark = sorted((_luminance(section["foreground"]), _luminance(section["background"])), reverse=True)
        assert (light + .05) / (dark + .05) >= 4.5


def test_dose_visible_viewport_layout(polish_page):
    page, _, view = polish_page
    for width in (320, 390, 1050, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth && document.body.scrollWidth <= innerWidth")
        assert page.locator("#header > div").evaluate_all("els => els.every(el => {const r=el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && el.scrollWidth <= el.clientWidth;})")
        header_height = page.locator("#header").bounding_box()["height"]
        assert header_height <= (88 if width >= 1440 else 190)
        expect(page.locator("#header h1")).to_be_visible()
        expect(page.locator("#view-switch")).to_be_visible()
        if width <= 390:
            page.route("**/api/auth/status", lambda route: route.fulfill(
                json={"mode": "oauth", "authenticated": False}))
            page.evaluate("loadAuthStatus()")
            login = page.locator("#auth-login")
            expect(login).to_be_visible()
            login_bounds = login.bounding_box()
            status_bounds = page.locator("#auth-state").bounding_box()
            assert login_bounds["x"] >= 0 and login_bounds["x"] + login_bounds["width"] <= width
            assert login_bounds["y"] >= status_bounds["y"] + status_bounds["height"]
            assert login.evaluate("el => el.scrollWidth <= el.clientWidth")
            page.unroute("**/api/auth/status")
            page.evaluate("loadAuthStatus()")
            expect(login).to_be_hidden()
        assert page.locator("#sidebar .step-card").evaluate_all("els => els.every(el => el.scrollWidth <= el.clientWidth)")
        clear_button = page.get_by_role("button", name="默认目录", exact=True)
        expect(clear_button).to_be_hidden()
        page.click("#output-location-options > summary")
        expect(clear_button).to_be_visible()
        expect(clear_button.locator("br")).to_have_count(0)
        assert clear_button.evaluate("el => {const range=document.createRange(); range.selectNodeContents(el); return range.getClientRects().length === 1 && el.scrollWidth <= el.clientWidth;}")
        page.click("#output-location-options > summary")
        _capture_viewport(page, f"dose-{view}-{width}")
        if view == "portal":
            summary = page.locator(".portal-products summary")
            summary.focus()
            summary.press("Enter")
            menu = page.locator(".portal-product-menu")
            expect(menu).to_be_visible()
            bounds = menu.bounding_box()
            assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
            assert bounds["y"] >= 0 and bounds["y"] + bounds["height"] <= 900
            anchor = page.locator("#header" if width <= 700 else "#header-product").bounding_box()
            assert bounds["y"] >= anchor["y"] + anchor["height"]
            assert _menu_hit(page, ".portal-product-menu")["menuOnTop"]
            _capture_viewport(page, f"dose-menu-{width}")
            page.locator("#portal-aef").uncheck()
            expect(page.locator("#portal-selected")).to_have_text("请选择目标数据")
            assert summary.evaluate("el => el.scrollWidth <= el.clientWidth && el.getBoundingClientRect().right <= innerWidth")
            page.locator("#portal-aef").check()
            page.keyboard.press("Escape")
            expect(menu).to_be_hidden()
            expect(summary).to_be_focused()


def test_basemap_svg_click_and_escape(polish_page):
    page, _, _ = polish_page
    toggle = page.locator("#toolmap")
    toggle.locator("svg").click()
    expect(toggle).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#basemap-menu")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#basemap-menu")).to_be_hidden()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(toggle).to_be_focused()


@pytest.mark.parametrize("polish_page", ["portal"], indirect=True)
def test_product_menu_is_above_leaflet_controls_markers_and_popups(polish_page):
    page, _, _ = polish_page
    page.set_viewport_size({"width": 1440, "height": 900})
    page.click(".portal-products summary")
    control_hit = _menu_hit(page, ".leaflet-control-zoom-in")
    assert control_hit["overlap"] and control_hit["menuOnTop"], control_hit
    zoom = page.evaluate("map.getZoom()")
    page.mouse.click(control_hit["x"], control_hit["y"])
    assert page.evaluate("map.getZoom()") == zoom
    page.locator("#portal-aef").check()

    page.evaluate("""() => {
      const label = document.querySelector('.portal-product-menu label').getBoundingClientRect();
      const bounds = document.getElementById('map').getBoundingClientRect();
      const x = (Math.max(label.left, bounds.left) + Math.min(label.right, bounds.right)) / 2;
      const y = (Math.max(label.top, bounds.top) + Math.min(label.bottom, bounds.bottom)) / 2;
      const latlng = map.containerPointToLatLng([x - bounds.left, y - bounds.top]);
      window.doseMarkerClicked = false;
      L.marker(latlng, {icon:L.divIcon({className:'leaflet-div-icon dose-marker-probe', html:'M', iconSize:[32,32], iconAnchor:[16,16]})})
        .on('click', () => {window.doseMarkerClicked = true;}).addTo(map);
    }""")
    marker_hit = _menu_hit(page, ".dose-marker-probe")
    assert marker_hit["overlap"] and marker_hit["menuOnTop"], marker_hit
    page.mouse.click(marker_hit["x"], marker_hit["y"])
    expect(page.locator("#portal-aef")).not_to_be_checked()
    assert not page.evaluate("window.doseMarkerClicked")
    page.locator("#portal-aef").check()

    page.evaluate("""() => {
      const menu = document.querySelector('.portal-product-menu').getBoundingClientRect();
      const bounds = document.getElementById('map').getBoundingClientRect();
      const x = (Math.max(menu.left, bounds.left) + Math.min(menu.right, bounds.right)) / 2;
      const y = Math.min(menu.bottom, bounds.bottom) - 8;
      L.popup({autoPan:false, closeButton:false, className:'dose-popup-probe'})
        .setLatLng(map.containerPointToLatLng([x - bounds.left, y - bounds.top]))
        .setContent('Overlay probe').openOn(map);
    }""")
    expect(page.locator(".dose-popup-probe")).to_be_visible()
    popup_hit = _menu_hit(page, ".dose-popup-probe")
    assert popup_hit["overlap"] and popup_hit["menuOnTop"], popup_hit
    _capture_viewport(page, "dose-menu-over-leaflet")

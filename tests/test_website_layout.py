from __future__ import annotations

import os
from pathlib import Path

from test_website_first_run import website_browser  # noqa: F401


def test_homepage_layout_and_keyboard_entry(website_browser):
    browser, url = website_browser
    context = browser.new_context(reduced_motion='reduce')
    page = context.new_page()
    for width, height in ((320, 844), (390, 844), (1280, 800), (1440, 900), (1920, 1080)):
        page.set_viewport_size({'width': width, 'height': height})
        page.goto(url)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('.hero h1').inner_text() == 'AEF数据\n端到端直传本地'
        author = page.get_by_role('link', name='by Kae', exact=True)
        assert author.get_attribute('href') == 'https://orcid.org/0000-0001-8443-9819'
        assert author.get_attribute('target') == '_blank'
        assert 'noopener' in author.get_attribute('rel')
        facts = page.locator('.hero .strip > span')
        assert facts.locator('strong').all_text_contents() == ['本地保存', '跨平台', '全球', '64 维']
        assert facts.nth(2).inner_text() == '全球\n10m格网像元'
        boxes = [item.bounding_box() for item in facts.all()]
        assert all(abs(box['x'] - boxes[0]['x']) < 1 for box in boxes)
        assert all(b['y'] >= a['y'] + a['height'] for a, b in zip(boxes, boxes[1:]))
        if width >= 1280:
            for selector in ('.hero h1', '.hero .lead', '.hero .actions'):
                bounds = page.locator(selector).bounding_box()
                assert bounds['y'] >= 0 and bounds['y'] + bounds['height'] <= height
        if width == 1920:
            bounds = page.locator('main').bounding_box()
            assert bounds['width'] <= 1200 and bounds['x'] >= 120
        for selector in ('.hero .button', '.nav-links a', '.brand-line a', '.hero .strip > span'):
            for element in page.locator(selector).all():
                assert element.evaluate('el => el.scrollWidth <= el.clientWidth')
                bounds = element.bounding_box()
                assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
        if evidence := os.environ.get('AEF_GRITS_ACCEPTANCE_DIR'):
            directory = Path(evidence)
            directory.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(directory / f'hero-{width}.png'))
        page.keyboard.press('Tab')
        assert page.locator('.skip').evaluate("el => el === document.activeElement && getComputedStyle(el).outlineStyle !== 'none'")
        page.keyboard.press('Enter')
        assert page.url.endswith('#main')
    context.close()


def test_privacy_footer_stays_compact_and_expands_without_clipping(website_browser):
    browser, url = website_browser
    context = browser.new_context(reduced_motion='reduce')
    page = context.new_page()
    for width in (320, 390, 1440):
        page.set_viewport_size({'width': width, 'height': 900})
        page.goto(url)
        privacy = page.locator('.privacy')
        summary = page.locator('#privacy-title')
        collapsed_height = privacy.bounding_box()['height']
        assert collapsed_height <= (180 if width < 760 else 100)
        assert page.locator('.footer').bounding_box()['height'] <= 170
        for expanded in (False, True):
            if expanded:
                summary.focus()
                page.keyboard.press('Enter')
                assert page.locator('.privacy details').get_attribute('open') is not None
                assert privacy.bounding_box()['height'] > collapsed_height
                for paragraph in page.locator('.privacy details p').all():
                    assert paragraph.is_visible()
                    assert paragraph.bounding_box()['y'] >= privacy.bounding_box()['y']
                    assert paragraph.bounding_box()['y'] + paragraph.bounding_box()['height'] <= privacy.bounding_box()['y'] + privacy.bounding_box()['height']
            for element in page.locator('.author-links > *, .footer a, .footer p, .footer-top span').all():
                bounds = element.bounding_box()
                assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
            assert page.locator('.footer').bounding_box()['y'] >= privacy.bounding_box()['y'] + privacy.bounding_box()['height']
            if evidence := os.environ.get('AEF_GRITS_ACCEPTANCE_DIR'):
                directory = Path(evidence)
                directory.mkdir(parents=True, exist_ok=True)
                privacy.evaluate("el => el.scrollIntoView({block: 'start', behavior: 'instant'})")
                page.screenshot(path=str(directory / f'footer-{width}-{"open" if expanded else "closed"}.png'))
        summary.focus()
        page.keyboard.press('Enter')
        assert page.locator('.privacy details').get_attribute('open') is None
        page.wait_for_function(
            "height => Math.abs(document.querySelector('.privacy').getBoundingClientRect().height - height) < 1",
            arg=collapsed_height,
        )
    context.close()

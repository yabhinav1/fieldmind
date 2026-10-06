"""The dashboard in a real browser: the page loads, unlocks, records, searches and
shows no console errors. Runs when Playwright and its Chromium are installed
(`pip install playwright && playwright install chromium`); skipped otherwise."""

import socket
import threading
import time

import httpx
import pytest

from fieldmind.api import create_app

playwright = pytest.importorskip("playwright.sync_api")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(fleet):
    """One device served over real HTTP on localhost, as the browser needs."""
    import uvicorn

    device = fleet("edge-a")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(device=device), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{base}/healthz", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield base, device
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def page():
    with playwright.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as error:  # Chromium not installed
            pytest.skip(f"Playwright's Chromium is not available: {str(error).splitlines()[0]}")
        context = browser.new_context(viewport={"width": 1366, "height": 900})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # The first poll runs before the device is unlocked and is answered 401 by
        # design; Chromium logs that as a console error, so it is not counted.
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text else None)
        page.errors = errors
        yield page
        browser.close()


def test_the_dashboard_works_in_a_browser(served, page):
    base, device = served
    page.goto(base)
    # First run on the device itself: choose a PIN, no setup code needed.
    page.wait_for_selector("#lock:not([hidden])")
    assert page.is_hidden("#lock-code")
    page.fill("#lock-pin", "2468")
    page.fill("#lock-confirm", "2468")
    page.click("#lock-submit")
    page.wait_for_selector("#lock[hidden]", state="attached")

    # Empty device: the start card shows, the settings menu does not.
    page.wait_for_selector("#start-here:not([hidden])")
    assert page.is_hidden("#settings-menu")
    assert page.is_hidden("#photo-preview")

    # Record a note and watch the policy preview.
    page.fill("#note", "Motor M-9 overheating, call Operator Anil Sharma on 9876543210 before restarting")
    page.wait_for_selector(".preview.redacted")
    assert "[person removed]" in page.inner_text("#preview")
    page.click("#save-note")
    page.wait_for_selector("#capture-result .notice")
    page.wait_for_selector("#start-here[hidden]", state="attached")

    # Search finds it on the device.
    page.fill("#query", "which motor is overheating")
    page.click("#search-form button[type=submit]")
    page.wait_for_selector("#results .item")
    assert "M-9" in page.inner_text("#results .item")
    assert "0 network calls" in page.inner_text("#search-meta")

    # Tabs, the settings menu and the guide render.
    page.click('.tab[data-tab="sync"]')
    page.wait_for_selector("#panel-sync.active")
    page.click("#settings")
    page.wait_for_selector("#settings-menu:not([hidden])")
    page.click("#change-pin")
    page.wait_for_selector("#pin-form")
    page.keyboard.press("Escape")
    page.click("#guide")
    page.wait_for_selector(".guide-step")
    assert len(page.query_selector_all(".guide-step")) == 8

    assert not page.errors, page.errors

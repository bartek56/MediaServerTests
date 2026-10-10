"""Optional browser checks. Playwright is deliberately not a base dependency."""

from __future__ import annotations

import re

import pytest

from conftest import AcceptanceConfig, WebUIEndpoint, redact


pytestmark = pytest.mark.acceptance


def _screenshot_path(endpoint: WebUIEndpoint, directory) -> object:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", endpoint.name).strip("._") or "endpoint"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe_name}.png"


def test_configured_web_ui(test_config: AcceptanceConfig) -> None:
    settings = test_config.web_ui
    if not settings.enabled:
        pytest.skip("web UI checks are disabled")
    if not settings.endpoints:
        pytest.skip("web UI is enabled but no endpoints are configured")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("Playwright is not installed; install requirements-browser.txt for web UI checks")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        context = browser.new_context()
        page = context.new_page()
        console_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(redact(message.text)) if message.type == "error" else None)
        page.on("pageerror", lambda error: console_errors.append(redact(str(error))))
        try:
            for endpoint in settings.endpoints:
                try:
                    page.goto(endpoint.url, wait_until="load", timeout=10000)
                    assert page.url, f"web UI endpoint {endpoint.name} did not load"
                    for selector in endpoint.selectors:
                        assert page.locator(selector).count() > 0, (
                            f"web UI endpoint {endpoint.name} is missing configured element"
                        )
                    for interaction in endpoint.interactions:
                        action = interaction.get("action")
                        selector = interaction.get("selector")
                        if action not in {"click", "fill", "press"} or not selector:
                            pytest.fail(f"invalid interaction configured for web UI endpoint {endpoint.name}")
                        locator = page.locator(str(selector)).first
                        if action == "click":
                            locator.click()
                        elif action == "fill":
                            locator.fill(str(interaction.get("value", "")))
                        else:
                            locator.press(str(interaction.get("value", "Enter")))
                    if settings.fail_on_console_errors and console_errors:
                        pytest.fail(f"JavaScript errors were reported by web UI endpoint {endpoint.name}")
                except Exception:
                    if endpoint.screenshot_on_failure:
                        page.screenshot(path=str(_screenshot_path(endpoint, settings.screenshot_dir)), full_page=True)
                    raise
        finally:
            try:
                context.close()
            finally:
                browser.close()

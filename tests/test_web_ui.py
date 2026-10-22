"""Optional browser checks. Playwright is deliberately not a base dependency."""

from __future__ import annotations

import re
import logging
import time

import pytest

from conftest import AcceptanceConfig, WebUIEndpoint, redact


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def _screenshot_path(endpoint: WebUIEndpoint, directory) -> object:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", endpoint.name).strip("._") or "endpoint"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe_name}.png"


def _wait_before_screenshot() -> None:
    time.sleep(0.5)


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
        context = browser.new_context(ignore_https_errors=not settings.verify_tls)
        page = context.new_page()
        console_errors: list[str] = []

        def capture_console(message) -> None:
            if message.type == "error":
                console_errors.append(f"console error: {redact(message.text)}")

        def capture_page_error(error) -> None:
            console_errors.append(f"page error: {redact(str(error))}")

        def capture_failed_response(response) -> None:
            if response.status >= 400:
                console_errors.append(
                    f"HTTP {response.status} {response.request.method}: {redact(response.url)}"
                )

        page.on("console", capture_console)
        page.on("pageerror", capture_page_error)
        page.on("response", capture_failed_response)
        endpoint_failures: list[str] = []
        try:
            for endpoint in settings.endpoints:
                try:
                    # Errors from a previous endpoint must not affect this one.
                    console_errors.clear()
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
                            raise AssertionError(
                                f"invalid interaction configured for web UI endpoint {endpoint.name}"
                            )
                        locator = page.locator(str(selector)).first
                        if action == "click":
                            locator.click()
                        elif action == "fill":
                            locator.fill(str(interaction.get("value", "")))
                        else:
                            locator.press(str(interaction.get("value", "Enter")))
                    ignored_console_errors = [
                        error
                        for error in console_errors
                        if any(pattern in error for pattern in endpoint.ignore_console_errors)
                    ]
                    unexpected_console_errors = [
                        error for error in console_errors if error not in ignored_console_errors
                    ]
                    if ignored_console_errors:
                        LOGGER.info(
                            "Web UI endpoint ignored expected errors: name=%s errors=%s",
                            endpoint.name,
                            ignored_console_errors,
                        )
                    if settings.fail_on_console_errors and unexpected_console_errors:
                        raise AssertionError(
                            f"JavaScript errors were reported by web UI endpoint {endpoint.name}:\n"
                            + "\n".join(f"  - {error}" for error in unexpected_console_errors)
                        )
                    if endpoint.screenshot_on_success:
                        _wait_before_screenshot()
                        screenshot = _screenshot_path(endpoint, settings.screenshot_dir)
                        page.screenshot(path=str(screenshot), full_page=True)
                    else:
                        screenshot = None
                    LOGGER.info(
                        "Web UI endpoint result: name=%s status=passed url=%s screenshot=%s",
                        endpoint.name,
                        endpoint.url,
                        screenshot,
                    )
                except Exception as error:
                    screenshot = None
                    if endpoint.screenshot_on_failure:
                        _wait_before_screenshot()
                        screenshot = _screenshot_path(endpoint, settings.screenshot_dir)
                        page.screenshot(path=str(screenshot), full_page=True)
                    error_text = redact(str(error))
                    LOGGER.info(
                        "Web UI endpoint result: name=%s status=failed url=%s error=%s screenshot=%s",
                        endpoint.name,
                        endpoint.url,
                        error_text,
                        screenshot,
                    )
                    endpoint_failures.append(f"{endpoint.name}: {error_text}")
            if endpoint_failures:
                pytest.fail("Web UI endpoint checks failed:\n" + "\n".join(
                    f"  - {failure}" for failure in endpoint_failures
                ))
        finally:
            try:
                context.close()
            finally:
                browser.close()

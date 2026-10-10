"""HTTP/HTTPS checks performed from the Ubuntu test computer."""

from __future__ import annotations

import logging
import time

import requests
import pytest

from conftest import AcceptanceConfig, ConfigError, json_field, redact, request_endpoint, safe_url


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def test_configured_http_endpoints(
    test_config: AcceptanceConfig,
    http_session: requests.Session,
    record_property,
) -> None:
    if not test_config.http.endpoints:
        pytest.skip("no HTTP/HTTPS endpoints configured")
    for endpoint in test_config.http.endpoints:
        started = time.monotonic()
        LOGGER.info("HTTP endpoint check started: %s [%s]", endpoint.name, safe_url(endpoint.url))
        record_property("endpoint", endpoint.name)
        record_property("url", safe_url(endpoint.url))
        try:
            response = request_endpoint(http_session, test_config, endpoint)
        except ConfigError as exc:
            pytest.fail(str(exc))
        except requests.exceptions.Timeout:
            if not endpoint.required:
                pytest.skip(f"optional HTTP endpoint {endpoint.name} timed out")
            pytest.fail(f"HTTP endpoint {endpoint.name} timed out after {endpoint.timeout:.1f}s")
        except requests.exceptions.ConnectionError as exc:
            if not endpoint.required:
                pytest.skip(f"optional HTTP endpoint {endpoint.name} is unreachable")
            pytest.fail(f"HTTP endpoint {endpoint.name} is unreachable: {redact(str(exc))}")
        except requests.exceptions.RequestException as exc:
            if not endpoint.required:
                pytest.skip(f"optional HTTP endpoint {endpoint.name} request failed")
            pytest.fail(f"HTTP endpoint {endpoint.name} request failed: {redact(str(exc))}")

        elapsed_ms = (time.monotonic() - started) * 1000
        LOGGER.info(
            "HTTP endpoint result: %s status=%s expected=%s elapsed=%.0fms",
            endpoint.name,
            response.status_code,
            endpoint.expected_status,
            elapsed_ms,
        )
        record_property("status", response.status_code)
        record_property("expected_status", ", ".join(str(item) for item in endpoint.expected_status))
        record_property("elapsed_ms", f"{elapsed_ms:.0f}")
        if response.status_code not in endpoint.expected_status and not endpoint.required:
            pytest.skip(f"optional HTTP endpoint {endpoint.name} returned {response.status_code}")
        assert response.status_code in endpoint.expected_status, (
            f"HTTP endpoint {endpoint.name} returned {response.status_code}; "
            f"expected one of {endpoint.expected_status}"
        )
        if endpoint.content_type:
            actual_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            assert actual_type == endpoint.content_type.lower(), (
                f"HTTP endpoint {endpoint.name} returned content type {actual_type!r}; "
                f"expected {endpoint.content_type!r}"
            )
        for fragment in endpoint.contains:
            assert fragment in response.text, f"HTTP endpoint {endpoint.name} does not contain configured content"
        for header, expected in endpoint.headers.items():
            actual = response.headers.get(header)
            assert actual == expected, f"HTTP endpoint {endpoint.name} has unexpected {header} header"
        if endpoint.json_fields:
            try:
                document = response.json()
            except ValueError as exc:
                pytest.fail(f"HTTP endpoint {endpoint.name} did not return valid JSON: {exc}")
            for path, expected in endpoint.json_fields.items():
                try:
                    actual = json_field(document, path)
                except KeyError:
                    pytest.fail(f"HTTP endpoint {endpoint.name} is missing JSON field {path}")
                assert actual == expected, f"HTTP endpoint {endpoint.name} has unexpected value for JSON field {path}"

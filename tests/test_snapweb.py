"""Snapweb asset installation checks shared by the minimal and base images."""

from __future__ import annotations

import logging

import pytest

from conftest import SSHConnection


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def test_snapweb_sources(ssh_client: SSHConnection) -> None:
    """Verify that the built Snapweb web assets are installed on the target."""

    result = ssh_client.run([
        "sh",
        "-c",
        "root=/usr/share/snapserver/snapweb; "
        "test -d \"$root/assets\" && "
        "for file in "
        "apple-touch-icon-180x180.png favicon.ico index.html logo.svg "
        "maskable-icon-512x512.png pwa-512x512.png registerSW.js sw.js; do "
        "test -f \"$root/$file\" || exit 1; "
        "done; "
        "find \"$root\" -maxdepth 1 -type f -print | sort",
    ])
    assets = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    LOGGER.info("Snapweb assets result: returncode=%s files=%s", result.returncode, assets)
    assert result.returncode == 0, (
        "Snapweb runtime assets are not installed below /usr/share/snapserver/snapweb: "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )
    assert assets, "Snapweb asset directory is empty"


def test_snapweb_http_endpoint(test_config, http_session) -> None:
    """Verify the Snapcast HTTP server serves the installed Snapweb entrypoint."""

    endpoint = next(
        (item for item in test_config.http.endpoints if item.name == "Snapcast web"),
        None,
    )
    if endpoint is None:
        pytest.skip("Snapcast web endpoint is not configured for this image")

    response = http_session.get(
        endpoint.url,
        timeout=endpoint.timeout,
        verify=endpoint.verify_tls,
        allow_redirects=endpoint.follow_redirects,
    )
    LOGGER.info("Snapweb HTTP result: status=%s url=%s", response.status_code, endpoint.url)
    assert response.status_code in endpoint.expected_status, (
        f"Snapcast web returned HTTP {response.status_code}; "
        f"expected one of {endpoint.expected_status}"
    )
    assert "<html" in response.text.lower(), "Snapcast web did not return an HTML document"

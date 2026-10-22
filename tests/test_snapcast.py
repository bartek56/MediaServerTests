"""Acceptance checks for Snapclient configuration."""

from __future__ import annotations

import re

import pytest

from conftest import AcceptanceConfig, SSHConnection


pytestmark = pytest.mark.acceptance


def test_snapclient_points_to_snapserver(
    test_config: AcceptanceConfig,
    ssh_client: SSHConnection,
) -> None:
    """Verify that Snapclient is configured to use the expected Snapserver."""

    settings = test_config.snapcast
    assert settings.server_host, "snapcast.server_host is required for the Snapclient configuration test"
    snapclient_config = ssh_client.run(["cat", "/etc/snapclient.conf"])
    assert snapclient_config.returncode == 0, (
        "could not read /etc/snapclient.conf: "
        f"{snapclient_config.stderr.strip() or snapclient_config.stdout.strip()}"
    )
    configured_host = None
    for line in snapclient_config.stdout.splitlines():
        match = re.match(r"^\s*host\s*=\s*(\S+)\s*$", line)
        if match:
            configured_host = match.group(1)
            break
    assert configured_host == settings.server_host, (
        f"Snapclient host={configured_host!r}; expected Snapserver {settings.server_host!r}"
    )

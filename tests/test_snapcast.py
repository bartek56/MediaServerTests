"""Acceptance checks for Snapclient configuration."""

from __future__ import annotations

import logging
import re

import pytest

from conftest import AcceptanceConfig, SSHConnection


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


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


def test_snapserver_sources_and_fifos(
    test_config: AcceptanceConfig,
    ssh_client: SSHConnection,
) -> None:
    """Verify that MPD, Bluetooth and PC Snapserver sources are configured and available."""

    settings = test_config.snapcast
    snapserver_config = ssh_client.run(["cat", "/etc/snapserver.conf"])
    assert snapserver_config.returncode == 0, (
        "could not read /etc/snapserver.conf: "
        f"{snapserver_config.stderr.strip() or snapserver_config.stdout.strip()}"
    )

    expected_sources = (
        (settings.expected_stream, settings.expected_source_path),
        (settings.bluetooth_stream, settings.bluetooth_source_path),
        (settings.pc_stream, settings.pc_source_path),
    )
    source_lines = [
        line.strip()
        for line in snapserver_config.stdout.splitlines()
        if line.strip().startswith("source =")
    ]

    for stream_name, fifo_path in expected_sources:
        matching_lines = [
            line for line in source_lines
            if f"pipe://{fifo_path}?" in line and f"name={stream_name}" in line
        ]
        assert matching_lines, (
            f"Snapserver source {stream_name!r} for FIFO {fifo_path!r} is missing; "
            f"configured sources={source_lines!r}"
        )

        fifo_check = ssh_client.run(["test", "-p", fifo_path])
        assert fifo_check.returncode == 0, (
            f"Snapserver source {stream_name!r} FIFO {fifo_path!r} is not a named pipe: "
            f"{fifo_check.stderr.strip() or fifo_check.stdout.strip()}"
        )
        LOGGER.info("Snapserver source result: stream=%s fifo=%s", stream_name, fifo_path)

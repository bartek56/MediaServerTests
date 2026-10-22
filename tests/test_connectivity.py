"""Connectivity checks executed from the Ubuntu test computer."""

from __future__ import annotations

import socket
import logging

import pytest

from conftest import (
    SSHAuthenticationError,
    SSHConnection,
    SSHError,
    SSHHostKeyError,
    SSHUnavailableError,
    AcceptanceConfig,
)


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def test_ssh_connectivity(test_config: AcceptanceConfig) -> None:
    connection = SSHConnection(test_config.ssh)
    try:
        try:
            connection.connect()
        except SSHAuthenticationError as exc:
            pytest.fail(f"SSH authentication failed: {exc}")
        except SSHHostKeyError as exc:
            pytest.fail(f"SSH host key validation failed: {exc}")
        except SSHUnavailableError as exc:
            pytest.fail(f"Raspberry Pi target is not reachable over SSH: {exc}")
        except SSHError as exc:
            pytest.fail(f"SSH connection failed: {exc}")
        LOGGER.info("SSH connectivity result: %s@%s:%s authenticated and host key accepted",
                    test_config.ssh.username, test_config.ssh.host, test_config.ssh.port)
    finally:
        connection.close()


def test_configured_tcp_ports(
    test_config: AcceptanceConfig,
    socket_connector,
) -> None:
    if not test_config.tcp_ports:
        pytest.skip("no TCP ports configured")
    for check in test_config.tcp_ports:
        try:
            with socket_connector((check.host, check.port), timeout=check.timeout):
                LOGGER.info("TCP port result: %s %s:%s reachable", check.name, check.host, check.port)
        except socket.timeout as exc:
            pytest.fail(f"TCP service {check.name} timed out after {check.timeout:.1f}s: {exc}")
        except OSError as exc:
            pytest.fail(f"TCP service {check.name} is unavailable at {check.host}:{check.port}: {exc}")

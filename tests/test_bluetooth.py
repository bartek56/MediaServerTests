"""Bluetooth service and discoverability checks."""

from __future__ import annotations

import logging
import re

import pytest

from conftest import SSHConnection


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def test_bluetooth_service_active(ssh_client: SSHConnection) -> None:
    """Verify that the Bluetooth-to-Snapcast autoplug service is running."""

    result = ssh_client.run(["systemctl", "is-active", "bt-snapcast-autoplug.service"])
    state = result.stdout.strip() or result.stderr.strip() or "unknown"
    LOGGER.info("Bluetooth autoplug service result: state=%s", state)
    assert result.returncode == 0 and state == "active", (
        "bt-snapcast-autoplug.service is not active: "
        f"{state}; {result.stderr.strip() or result.stdout.strip()}"
    )


def test_bluetooth_discoverable(ssh_client: SSHConnection) -> None:
    """Verify that the active BlueZ adapter is discoverable."""

    result = ssh_client.run(["bluetoothctl", "show"])
    output = result.stdout + result.stderr
    match = re.search(r"^\s*Discoverable:\s*(yes|no)\s*$", output, re.MULTILINE)
    LOGGER.info(
        "Bluetooth discoverability result: returncode=%s discoverable=%s",
        result.returncode,
        match.group(1) if match else "unknown",
    )
    assert result.returncode == 0, (
        "bluetoothctl show failed: "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )
    assert match is not None, "bluetoothctl show did not report Discoverable state"
    assert match.group(1) == "yes", (
        "Bluetooth adapter is not discoverable; bluetoothctl show reported "
        f"Discoverable: {match.group(1)}"
    )

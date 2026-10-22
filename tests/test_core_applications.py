"""Core application checks shared by the supported Raspberry Pi images."""

from __future__ import annotations

import logging
import shutil
import subprocess

import pytest

from conftest import AcceptanceConfig, SSHConnection


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def test_raspberry_pi_identity(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> None:
    model = ssh_client.run(["cat", "/proc/device-tree/model"])
    if model.returncode != 0:
        pytest.skip("Raspberry Pi model information is unavailable")
    model_name = model.stdout.replace("\x00", "").strip()
    expected_model = {
        "rpi0": "Raspberry Pi Zero 2 W",
        "rpi3": "Raspberry Pi 3",
        "rpi4": "Raspberry Pi 4",
    }[test_config.machine]
    LOGGER.info("Raspberry Pi identity: model=%s", model_name)
    assert expected_model in model_name, f"unexpected target model for {test_config.machine}: {model_name}"


def test_opkg_configuration(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> None:
    """Verify that opkg points at feeds built for the configured machine."""

    expected_feeds = {
        "rpi0": ("cortexa53-nocrypto", "raspberrypi0_2w_64"),
        "rpi3": ("cortexa53-nocrypto", "raspberrypi3_64"),
        "rpi4": ("cortexa72", "raspberrypi4_64"),
    }[test_config.machine]
    result = ssh_client.run(["cat", "/etc/opkg/opkg.conf"])
    assert result.returncode == 0, (
        "could not read /etc/opkg.conf: "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )
    sources = {
        parts[1]: parts[2]
        for line in result.stdout.splitlines()
        if (parts := line.split()) and len(parts) == 3 and parts[0] == "src/gz"
    }
    for feed in expected_feeds:
        assert feed in sources, (
            f"opkg feed {feed!r} is missing for {test_config.machine}: "
            f"configured feeds={sorted(sources)}"
        )
    assert sources[expected_feeds[1]].rstrip("/").endswith(f"/{expected_feeds[1]}"), sources

    update = ssh_client.run(["opkg", "update"])
    update_output = (update.stdout + update.stderr).strip()
    assert update.returncode == 0, (
        f"opkg update failed for {test_config.machine} "
        f"(exit code {update.returncode})\n{update_output or '(no output)'}"
    )


def test_pulseaudio_tcp_info(test_config: AcceptanceConfig) -> None:
    settings = test_config.pulseaudio
    pactl = shutil.which("pactl")
    if pactl is None:
        pytest.fail("pactl is not installed on the Ubuntu test computer; install pulseaudio-utils")
    server = f"tcp:{test_config.ssh.host}:{settings.tcp_port}"
    try:
        result = subprocess.run([pactl, "-s", server, "info"], capture_output=True, text=True,
                                timeout=test_config.ssh.connect_timeout, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail(f"pactl timed out while connecting to PulseAudio server {server}")
    fields = {
        key.strip(): value.strip()
        for line in result.stdout.splitlines() if ":" in line
        for key, value in [line.split(":", 1)]
    }
    assert result.returncode == 0, f"pactl could not connect to {server}: {result.stderr.strip()}"
    assert fields.get("Server String") == server, fields
    assert fields.get("Library Protocol Version") == settings.expected_protocol_version, fields
    assert fields.get("Server Protocol Version") == settings.expected_protocol_version, fields
    assert fields.get("Is Local") == "no", fields
    assert fields.get("User Name") == settings.expected_user_name, fields
    assert fields.get("Server Name") == settings.expected_server_name, fields
    assert fields.get("Default Sample Specification") == settings.expected_sample_specification, fields
    assert fields.get("Default Sink") == settings.expected_default_sink, fields
    assert fields.get("Default Source") == settings.expected_default_source, fields

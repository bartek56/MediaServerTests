"""Functional smoke tests for applications in mediaserver-image-base."""

from __future__ import annotations

import socket
import logging
import re
import shutil
import subprocess
import time

import pytest
import requests

from conftest import AcceptanceConfig, ConfigError, SSHConnection, redact, request_endpoint


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def _port_named(config: AcceptanceConfig, name: str):
    for check in config.tcp_ports:
        if check.name.lower() == name.lower():
            return check
    return None


def test_raspberry_pi_4_identity(ssh_client: SSHConnection) -> None:
    model = ssh_client.run(["cat", "/proc/device-tree/model"])
    if model.returncode != 0:
        pytest.skip("Raspberry Pi model information is unavailable")
    model_name = model.stdout.replace("\x00", "").strip()
    LOGGER.info("Raspberry Pi identity: model=%s", model_name)
    assert "Raspberry Pi 4" in model.stdout, f"unexpected target model: {model.stdout.strip()}"


def test_mpd_protocol_smoke(test_config: AcceptanceConfig, socket_connector) -> None:
    check = _port_named(test_config, "MPD")
    if check is None:
        pytest.skip("MPD port is not configured")
    try:
        with socket_connector((check.host, check.port), timeout=check.timeout) as connection:
            greeting = connection.recv(128).decode("utf-8", errors="replace")
    except socket.timeout:
        pytest.fail("MPD handshake timed out")
    except OSError as exc:
        pytest.fail(f"MPD is not reachable: {exc}")
    LOGGER.info("MPD smoke result: %s:%s greeting=%r", check.host, check.port, greeting.strip())
    assert greeting.startswith("OK MPD"), f"unexpected MPD greeting: {greeting!r}"


def test_mpd_mpc_playback(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> None:
    """Verify the actual mpc play path, not only the MPD TCP greeting."""

    settings = test_config.mpd

    def run_mpc(*args: str):
        return ssh_client.run(["mpc", *args])

    def require_success(result, action: str) -> None:
        if result.returncode != 0:
            pytest.fail(f"mpc {action} failed: {redact(result.stderr.strip() or result.stdout.strip())}")

    status_pattern = re.compile(
        r"\[(?P<state>playing|paused|stopped)\].*?"
        r"(?P<elapsed_min>\d+):(?P<elapsed_sec>\d{2})/"
        r"(?P<total_min>\d+):(?P<total_sec>\d{2})"
    )
    original_playlist = run_mpc("playlist")
    require_success(original_playlist, "playlist")
    backup_name: str | None = None
    if original_playlist.stdout.strip():
        backup_name = f"acceptance-mpd-{int(time.time())}"
        backup = run_mpc("save", backup_name)
        require_success(backup, f"save {backup_name}")

    try:
        library = run_mpc("listall")
        require_success(library, "listall")
        tracks = [line.strip() for line in library.stdout.splitlines() if line.strip()]
        assert tracks, "MPD database contains no tracks"
        LOGGER.info("MPD/mpc library result: tracks=%d", len(tracks))

        require_success(run_mpc("clear"), "clear")
        first_track = tracks[0]
        added = run_mpc("add", first_track)
        require_success(added, "add first track")
        playlist = run_mpc("playlist")
        require_success(playlist, "playlist after add")
        playlist_entries = [line.strip() for line in playlist.stdout.splitlines() if line.strip()]
        assert len(playlist_entries) == 1, (
            f"mpc added {len(playlist_entries)} playlist entries, expected 1"
        )
        LOGGER.info("MPD/mpc playlist result: entries=%d first_entry=%r",
                    len(playlist_entries), playlist_entries[0])

        require_success(run_mpc("play", "1"), "play first track")
        deadline = time.monotonic() + settings.startup_timeout
        last_status = ""
        elapsed_seconds = 0
        state = "unknown"
        while time.monotonic() < deadline:
            status = run_mpc("status")
            require_success(status, "status")
            last_status = status.stdout.strip()
            match = status_pattern.search(last_status)
            if match:
                state = match.group("state")
                elapsed_seconds = int(match.group("elapsed_min")) * 60 + int(match.group("elapsed_sec"))
                if state == "playing" and elapsed_seconds > 0:
                    break
            time.sleep(settings.poll_interval)

        LOGGER.info(
            "MPD/mpc playback result: first_track=%r state=%s elapsed=%ss status=%r",
            playlist_entries[0],
            state,
            elapsed_seconds,
            last_status,
        )
        assert state == "playing", f"mpc did not start playback; final status: {last_status!r}"
        assert elapsed_seconds > 0, f"mpc reported no playback progress; final status: {last_status!r}"
    finally:
        # Restore the pre-test playlist so acceptance testing does not leave a
        # synthetic track in the user's MPD queue.
        stop = run_mpc("stop")
        if stop.returncode != 0:
            LOGGER.warning("mpc cleanup stop failed: %s", redact(stop.stderr.strip()))
        clear = run_mpc("clear")
        if clear.returncode != 0:
            LOGGER.warning("mpc cleanup clear failed: %s", redact(clear.stderr.strip()))
        if backup_name is not None:
            load = run_mpc("load", backup_name)
            if load.returncode != 0:
                LOGGER.warning("mpc cleanup load failed: %s", redact(load.stderr.strip()))
            remove = run_mpc("rm", backup_name)
            if remove.returncode != 0:
                LOGGER.warning("mpc cleanup remove failed: %s", redact(remove.stderr.strip()))


def test_certbot_dependencies(ssh_client: SSHConnection) -> None:
    """Verify that certbot starts and can load its installed dependencies."""

    checks = (
        ("version", ["certbot", "--version"]),
        ("plugins", ["certbot", "plugins"]),
        ("certificates", ["certbot", "certificates"]),
    )
    results = {}
    for name, command in checks:
        result = ssh_client.run(command)
        output = result.stdout.strip() or result.stderr.strip()
        results[name] = {"returncode": result.returncode, "output": redact(output)}
        assert result.returncode == 0, (
            f"certbot {name} failed on target: {redact(output)}"
        )

    assert re.search(r"certbot\s+\d+(?:\.\d+)+", results["version"]["output"], re.IGNORECASE), (
        f"certbot did not report a valid version: {results['version']['output']!r}"
    )
    LOGGER.info("Certbot dependency result: %s", results)


def _parse_pactl_info(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields


def test_pulseaudio_tcp_info(test_config: AcceptanceConfig) -> None:
    """Verify the remote PulseAudio TCP server and its default audio route."""

    pactl = shutil.which("pactl")
    if pactl is None:
        pytest.fail("pactl is not installed on the Ubuntu test computer; install pulseaudio-utils")

    settings = test_config.pulseaudio
    server = f"tcp:{test_config.ssh.host}:{settings.tcp_port}"
    try:
        result = subprocess.run(
            [pactl, "-s", server, "info"],
            capture_output=True,
            text=True,
            timeout=test_config.ssh.connect_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"pactl timed out while connecting to PulseAudio server {server}")
    output = result.stdout.strip()
    fields = _parse_pactl_info(output)
    log_fields = {key: value for key, value in fields.items() if key != "Cookie"}
    LOGGER.info("PulseAudio TCP result: server=%s returncode=%s fields=%s", server, result.returncode, log_fields)

    assert result.returncode == 0, (
        f"pactl could not connect to {server}: "
        f"{redact(result.stderr.strip() or output)}"
    )
    assert fields.get("Server String") == server, fields
    assert fields.get("Library Protocol Version") == settings.expected_protocol_version, fields
    assert fields.get("Server Protocol Version") == settings.expected_protocol_version, fields
    assert fields.get("Is Local") == "no", fields
    assert fields.get("User Name") == settings.expected_user_name, fields
    assert fields.get("Server Name") == settings.expected_server_name, fields
    assert fields.get("Default Sample Specification") == settings.expected_sample_specification, fields
    assert fields.get("Default Sink") == settings.expected_default_sink, fields
    assert fields.get("Default Source") == settings.expected_default_source, fields


def test_transmission_rpc_probe(test_config: AcceptanceConfig, http_session: requests.Session) -> None:
    endpoint = next((item for item in test_config.http.endpoints if item.name.startswith("Transmission RPC")), None)
    if endpoint is None:
        pytest.skip("Transmission RPC endpoint is not configured")
    response = request_endpoint(http_session, test_config, endpoint)
    LOGGER.info("Transmission RPC probe result: endpoint=%s status=%s", endpoint.name, response.status_code)
    assert response.status_code in {401, 403, 409}, (
        f"Transmission RPC returned unexpected status {response.status_code}"
    )
    if response.status_code == 409:
        assert response.headers.get("X-Transmission-Session-Id"), (
            "Transmission RPC did not return a session id for the unauthenticated handshake"
        )


def test_transmission_web_assets(ssh_client: SSHConnection) -> None:
    result = ssh_client.run([
        "sh",
        "-c",
        "find /usr/share/transmission -type f -name index.html -print 2>/dev/null",
    ])
    assets = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    LOGGER.info("Transmission web assets result: index_html=%s paths=%s", len(assets), assets)
    assert assets, (
        "Transmission web UI assets are missing on the target: "
        "no index.html was found below /usr/share/transmission"
    )


def test_transmission_web_ui(test_config: AcceptanceConfig, http_session: requests.Session) -> None:
    endpoint = next((item for item in test_config.http.endpoints if item.name == "Transmission web UI"), None)
    if endpoint is None:
        pytest.skip("Transmission web UI endpoint is not configured")

    try:
        response = request_endpoint(http_session, test_config, endpoint)
    except ConfigError as exc:
        pytest.fail(f"Transmission web UI authentication is not configured: {exc}")
    except requests.exceptions.RequestException as exc:
        pytest.fail(f"Transmission web UI is unreachable: {redact(str(exc))}")

    body = response.text
    missing_files_error = "Couldn't find Transmission's web interface files!"
    LOGGER.info(
        "Transmission web UI result: status=%s url=%s content_length=%s missing_files_error=%s",
        response.status_code,
        endpoint.url,
        len(body),
        missing_files_error in body,
    )
    assert response.status_code == 200, (
        f"Transmission web UI returned HTTP {response.status_code}; expected 200"
    )
    assert missing_files_error not in body, (
        "Transmission web UI is not installed/configured: "
        "the response reports missing Transmission web interface files"
    )
    assert "transmission" in body.lower(), "Transmission web UI response does not contain Transmission content"

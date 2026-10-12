"""Acceptance checks for the MPD -> Snapserver -> Snapclient audio route."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import pytest

from conftest import AcceptanceConfig, SSHConnection


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def _snapserver_status(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> dict[str, Any]:
    """Read Snapserver's runtime state through its HTTP JSON-RPC API."""

    # Snapcast 0.35 stores JSON-RPC ids in a 32-bit integer. Keep this small
    # instead of using Unix time in milliseconds, which would overflow there.
    request_id = 1
    request = {
        "id": request_id,
        "jsonrpc": "2.0",
        "method": "Server.GetStatus",
    }
    result = ssh_client.run([
        "curl",
        "-fsS",
        "-H",
        "Content-Type: application/json",
        "--data",
        json.dumps(request, separators=(",", ":")),
        f"http://127.0.0.1:{test_config.snapcast.http_port}/jsonrpc",
    ])
    if result.returncode != 0:
        pytest.fail(
            f"Snapserver HTTP JSON-RPC on port {test_config.snapcast.http_port} is unavailable: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"Snapserver JSON-RPC returned invalid JSON: {exc}")
    if not isinstance(response, dict) or response.get("id") != request_id:
        pytest.fail(f"Snapserver JSON-RPC returned an unexpected response: {response!r}")
    return response


def _default_source(config_text: str) -> str | None:
    for line in config_text.splitlines():
        match = re.match(r"^\s*default_source\s*=\s*(\S+)\s*(?:#.*)?$", line)
        if match:
            return match.group(1)
    return None


def _stream_sources(config_text: str) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    for line in config_text.splitlines():
        match = re.match(r"^\s*source\s*=\s*(\S+)", line)
        if not match:
            continue
        uri = match.group(1)
        name_match = re.search(r"(?:\?|&)name=([^&\s]+)", uri)
        if name_match:
            sources.append((name_match.group(1), uri))
    return sources


def test_snapclient_defaults_to_mpd_stream(
    test_config: AcceptanceConfig,
    ssh_client: SSHConnection,
) -> None:
    """Verify the configured and active Snapcast route for this Raspberry Pi."""

    settings = test_config.snapcast
    snapserver_config = ssh_client.run(["cat", "/etc/snapserver.conf"])
    assert snapserver_config.returncode == 0, (
        "could not read /etc/snapserver.conf: "
        f"{snapserver_config.stderr.strip() or snapserver_config.stdout.strip()}"
    )
    configured_default = _default_source(snapserver_config.stdout)
    configured_sources = _stream_sources(snapserver_config.stdout)
    assert configured_sources, "Snapserver configuration contains no active stream sources"
    if configured_default is not None:
        assert configured_default == settings.expected_stream, (
            f"Snapserver default_source={configured_default!r}; "
            f"expected {settings.expected_stream!r}"
        )
    else:
        # Snapcast 0.35 has no default_source key. Its first configured source
        # is the default until a group is explicitly switched at runtime.
        assert configured_sources[0][0] == settings.expected_stream, (
            f"Snapserver first configured source={configured_sources[0][0]!r}; "
            f"expected {settings.expected_stream!r}"
        )

    hostname_result = ssh_client.run(["hostname"])
    target_hostname = hostname_result.stdout.strip() if hostname_result.returncode == 0 else ""
    response = _snapserver_status(test_config, ssh_client)
    if "error" in response:
        pytest.fail(f"Snapserver JSON-RPC returned an error: {response['error']}")

    server = response.get("result", {}).get("server", {})
    streams = server.get("streams", [])
    groups = server.get("groups", [])
    expected_stream = next((item for item in streams if item.get("id") == settings.expected_stream), None)
    assert expected_stream is not None, (
        f"Snapserver does not expose the expected {settings.expected_stream!r} stream; "
        f"available={[item.get('id') for item in streams]}"
    )
    expected_uri = str(expected_stream.get("uri", {}).get("raw", ""))
    assert settings.expected_source_path in expected_uri, (
        f"{settings.expected_stream} stream is not backed by {settings.expected_source_path!r}: "
        f"uri={expected_uri!r}"
    )

    target_groups: list[dict[str, Any]] = []
    for group in groups:
        for client in group.get("clients", []):
            host = client.get("host", {})
            if host.get("ip") == test_config.ssh.host or (
                target_hostname and host.get("name") == target_hostname
            ):
                if client.get("connected"):
                    target_groups.append({"group": group, "client": client})

    assert target_groups, (
        "No connected Snapclient for the target Raspberry Pi was found in Snapserver status; "
        f"groups={groups!r}"
    )
    selected_streams = {item["group"].get("stream_id") for item in target_groups}
    client_ids = [item["client"].get("id") for item in target_groups]
    LOGGER.info(
        "Snapclient default routing result: http_jsonrpc=%s configured_default=%s "
        "configured_sources=%s stream=%s source=%s clients=%s bluetooth_stream=%s",
        f"127.0.0.1:{settings.http_port}/jsonrpc",
        configured_default or f"first:{configured_sources[0][0]}",
        [name for name, _uri in configured_sources],
        sorted(selected_streams),
        expected_uri,
        client_ids,
        settings.bluetooth_stream,
    )
    assert selected_streams == {settings.expected_stream}, (
        f"target Snapclient is assigned to {sorted(selected_streams)!r}; "
        f"expected only {settings.expected_stream!r}, not {settings.bluetooth_stream!r}"
    )

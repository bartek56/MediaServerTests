"""Unit tests for framework helpers; no Raspberry Pi or network is required."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from conftest import (
    ConfigError,
    SSHConnection,
    SSHSettings,
    HTTPEndpoint,
    AcceptanceConfig,
    HTTPSettings,
    json_field,
    load_config,
    parse_memory_kib,
    redact,
    request_endpoint,
)


pytestmark = pytest.mark.unit


def test_load_config_applies_environment_overrides(tmp_path: Path) -> None:
    config_file = tmp_path / "test.yaml"
    config_file.write_text(
        """
device:
  name: sample
  host: 192.0.2.10
  ssh:
    username: tester
    key_path: /tmp/test-key
connectivity:
  tcp_ports: [80]
""",
        encoding="utf-8",
    )
    config = load_config(config_file, {"RPI4_HOST": "192.0.2.11", "RPI4_SSH_PORT": "2200"})
    assert config.ssh.host == "192.0.2.11"
    assert config.ssh.port == 2200


def test_load_config_rejects_inline_credentials(tmp_path: Path) -> None:
    config_file = tmp_path / "test.yaml"
    config_file.write_text(
        """
device:
  host: 192.0.2.10
  ssh: {username: tester, key_path: /tmp/test-key}
http:
  endpoints:
    - {name: api, url: http://192.0.2.10/api, auth: {token: secret}}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="credentials"):
        load_config(config_file, {})


def test_ssh_run_quotes_arguments_and_returns_result() -> None:
    channel = Mock()
    channel.exit_status_ready.return_value = True
    channel.recv_ready.return_value = False
    channel.recv_stderr_ready.return_value = False
    channel.recv_exit_status.return_value = 7
    stdout = Mock(channel=channel)
    client = Mock()
    client.exec_command.return_value = (None, stdout, Mock())
    settings = SSHSettings("host", 22, "user", Path("key"), Path("known_hosts"), 1, 1)
    connection = SSHConnection(settings)
    connection.client = client
    result = connection.run(["printf", "hello; do-not-run"])
    assert result.returncode == 7
    client.exec_command.assert_called_once_with("printf 'hello; do-not-run'")


def test_helpers_parse_and_redact() -> None:
    assert parse_memory_kib("MemTotal: 1024 kB\nMemAvailable: 256 kB\n") == (256, 1024)
    redacted = redact("Authorization: Bearer abc password=xyz")
    assert "abc" not in redacted and "xyz" not in redacted
    assert json_field({"items": [{"state": "ok"}]}, "items.0.state") == "ok"


def test_http_request_uses_environment_token_without_exposing_it() -> None:
    from conftest import AuthSettings, SystemSettings, WebUISettings

    endpoint = HTTPEndpoint("api", "https://example.invalid", auth=AuthSettings(bearer_token_env="API_TOKEN"))
    config = AcceptanceConfig("device", SSHSettings("host", 22, "user", Path("key"), Path("known"), 1, 1), (), SystemSettings(), (), HTTPSettings(endpoints=(endpoint,)), WebUISettings())
    session = Mock()
    response = Mock()
    session.get.return_value = response
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("API_TOKEN", "secret-token")
        request_endpoint(session, config, endpoint)
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in str(endpoint)

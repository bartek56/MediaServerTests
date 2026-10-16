"""Shared configuration, SSH, HTTP and reporting helpers for acceptance tests.

The test runner is intentionally a host-side tool. Nothing from this module
is installed into the image tested on the Raspberry Pi.
"""

from __future__ import annotations

import os
import logging
import re
import select
import shlex
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import paramiko
import pytest
import requests
import yaml
from requests.auth import HTTPBasicAuth
from paramiko.ssh_exception import NoValidConnectionsError


class ConfigError(ValueError):
    """Raised when the acceptance configuration is missing or invalid."""


class SSHError(RuntimeError):
    """Base class for errors while connecting to or using SSH."""


class SSHUnavailableError(SSHError):
    """The host or SSH port could not be reached."""


class SSHAuthenticationError(SSHError):
    """The SSH server rejected the configured credentials."""


class SSHHostKeyError(SSHError):
    """The server key is not trusted by known_hosts."""


class SSHCommandTimeout(SSHError):
    """A remote command exceeded its configured timeout."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SSHSettings:
    host: str
    port: int
    username: str
    key_path: Path | None
    known_hosts: Path
    connect_timeout: float
    command_timeout: float
    password_env: str | None = None
    allow_empty_password: bool = False


@dataclass(frozen=True)
class PortCheck:
    name: str
    host: str
    port: int
    timeout: float


@dataclass(frozen=True)
class FilesystemCheck:
    path: str
    min_free_mb: int


@dataclass(frozen=True)
class SystemSettings:
    required_commands: tuple[str, ...] = ()
    filesystems: tuple[FilesystemCheck, ...] = ()
    min_available_memory_mb: int | None = None
    max_memory_used_percent: float | None = None
    min_uptime_seconds: int | None = None
    critical_log_patterns: tuple[str, ...] = ()
    log_source: str | None = None


@dataclass(frozen=True)
class ServiceCheck:
    name: str
    manager: str = "systemd"
    expected_state: str = "active"
    ports: tuple[PortCheck, ...] = ()
    diagnostics: bool = False
    required: bool = True


@dataclass(frozen=True)
class AuthSettings:
    bearer_token_env: str | None = None
    basic_username_env: str | None = None
    basic_password_env: str | None = None


@dataclass(frozen=True)
class HTTPEndpoint:
    name: str
    url: str
    expected_status: tuple[int, ...] = (200,)
    timeout: float = 10.0
    verify_tls: bool = True
    follow_redirects: bool = True
    content_type: str | None = None
    contains: tuple[str, ...] = ()
    headers: Mapping[str, str] = field(default_factory=dict)
    auth: AuthSettings = field(default_factory=AuthSettings)
    json_fields: Mapping[str, Any] = field(default_factory=dict)
    required: bool = True


@dataclass(frozen=True)
class WebUIEndpoint:
    name: str
    url: str
    selectors: tuple[str, ...] = ()
    interactions: tuple[Mapping[str, Any], ...] = ()
    screenshot_on_failure: bool = True
    screenshot_on_success: bool = False
    ignore_console_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class WebUISettings:
    enabled: bool = False
    headless: bool = True
    fail_on_console_errors: bool = True
    screenshot_dir: Path = Path("reports/screenshots")
    endpoints: tuple[WebUIEndpoint, ...] = ()


@dataclass(frozen=True)
class HTTPSettings:
    headers: Mapping[str, str] = field(default_factory=dict)
    headers_from_env: Mapping[str, str] = field(default_factory=dict)
    endpoints: tuple[HTTPEndpoint, ...] = ()


@dataclass(frozen=True)
class MPDSettings:
    startup_timeout: float = 8.0
    poll_interval: float = 0.5


@dataclass(frozen=True)
class SnapcastSettings:
    http_port: int = 1780
    expected_stream: str = "MPD"
    expected_source_path: str = "/tmp/snapmpdfifo"
    bluetooth_stream: str = "Bluetooth"


@dataclass(frozen=True)
class PulseAudioSettings:
    tcp_port: int = 4713
    expected_protocol_version: str = "35"
    expected_user_name: str = "pulse"
    expected_server_name: str = "pulseaudio"
    expected_sample_specification: str = "s16le 2ch 44100Hz"
    expected_default_sink: str = "alsa_output.headphones.stereo-fallback"
    expected_default_source: str = "alsa_output.headphones.stereo-fallback.monitor"


@dataclass(frozen=True)
class AcceptanceConfig:
    name: str
    ssh: SSHSettings
    tcp_ports: tuple[PortCheck, ...]
    system: SystemSettings
    services: tuple[ServiceCheck, ...]
    http: HTTPSettings
    web_ui: WebUISettings
    mpd: MPDSettings = field(default_factory=MPDSettings)
    snapcast: SnapcastSettings = field(default_factory=SnapcastSettings)
    pulseaudio: PulseAudioSettings = field(default_factory=PulseAudioSettings)


TEST_DESCRIPTIONS = {
    "test_raspberry_pi_4_identity": "Verifies that the SSH target identifies itself as Raspberry Pi 4.",
    "test_mpd_protocol_smoke": "Checks that MPD accepts a TCP connection and returns an MPD greeting.",
    "test_mpd_mpc_playback": "Adds all tracks from the MPD database with mpc, plays the first one and verifies advancing playback.",
    "test_snapclient_defaults_to_mpd_stream": "Checks Snapserver configuration and runtime client routing: the RPi snapclient must use MPD, not Bluetooth.",
    "test_snapweb_sources": "Checks that the Snapweb page sources are installed in the Snapcast document root.",
    "test_snapweb_http_endpoint": "Checks that Snapserver serves the installed Snapweb HTML entrypoint.",
    "test_pulseaudio_tcp_info": "Checks the PulseAudio TCP server through pactl and verifies its negotiated server and default audio settings.",
    "test_transmission_rpc_probe": "Checks that the Transmission RPC endpoint responds to an unauthenticated probe.",
    "test_transmission_web_assets": "Checks over SSH that the Transmission web UI includes an index.html asset on the target.",
    "test_transmission_web_ui": "Checks that the Transmission web interface returns a valid page instead of a missing-files error.",
    "test_ssh_connectivity": "Checks SSH reachability, host-key validation and authentication to the target.",
    "test_configured_tcp_ports": "Checks configured TCP ports from the Ubuntu test computer.",
    "test_system_information": "Reads basic remote kernel and system information through SSH.",
    "test_required_diagnostic_commands": "Checks availability of configured diagnostic commands on the target.",
    "test_configured_filesystems": "Checks free-space thresholds on configured remote filesystems.",
    "test_memory_thresholds": "Checks configured available-memory and memory-usage thresholds.",
    "test_uptime_threshold": "Checks the configured minimum target uptime when enabled.",
    "test_critical_log_patterns": "Checks bounded remote log output for configured critical patterns.",
    "test_configured_service_states": "Checks configured service-manager states and collects bounded diagnostics on failure.",
    "test_configured_service_ports": "Checks TCP ports associated with configured services.",
    "test_configured_http_endpoints": "Checks configured HTTP/HTTPS status, headers, content and JSON expectations.",
    "test_configured_web_ui": "Checks configured web pages, DOM selectors, interactions and JavaScript errors.",
    "test_load_config_applies_environment_overrides": "Checks YAML loading and documented environment overrides.",
    "test_load_config_rejects_inline_credentials": "Checks that inline HTTP credentials are rejected.",
    "test_ssh_run_quotes_arguments_and_returns_result": "Checks safe SSH argument quoting and command result handling.",
    "test_helpers_parse_and_redact": "Checks memory parsing, JSON lookup and credential redaction helpers.",
    "test_http_request_uses_environment_token_without_exposing_it": "Checks environment-backed HTTP bearer authentication handling.",
}
_HTML_HEADER_ADDED = False
_TARGET_BUILD_INFO = "unavailable"
_TRANSMISSION_TEST_DEFAULTS = {
    "TRANSMISSION_RPC_USERNAME": "transmission",
    "TRANSMISSION_RPC_PASSWORD": "transmission",
}


def _test_description(item: pytest.Item) -> str:
    description = TEST_DESCRIPTIONS.get(item.name)
    if description:
        return description
    function = getattr(item, "function", None)
    return (getattr(function, "__doc__", "") or "").strip() or item.name


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a mapping")
    return value


def _as_list(value: Any, label: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigError(f"{label} must be a list")
    return list(value)


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _number(value: Any, label: str, integer: bool = False) -> int | float:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be a number")
    try:
        result = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a number") from exc
    if result < 0:
        raise ConfigError(f"{label} must not be negative")
    return result


def _bool(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "yes", "1"}:
        return True
    if isinstance(value, str) and value.lower() in {"false", "no", "0"}:
        return False
    raise ConfigError(f"{label} must be boolean")


def _port_check(value: Any, label: str, default_host: str, default_timeout: float) -> PortCheck:
    data: Mapping[str, Any] = {"port": value} if isinstance(value, int) else _mapping(value, label)
    port = _number(data.get("port"), f"{label}.port", integer=True)
    if not 1 <= port <= 65535:
        raise ConfigError(f"{label}.port must be between 1 and 65535")
    host = str(data.get("host", default_host)).strip()
    if not host:
        raise ConfigError(f"{label}.host must not be empty")
    return PortCheck(
        name=str(data.get("name", f"{host}:{port}")),
        host=host,
        port=port,
        timeout=float(_number(data.get("timeout", default_timeout), f"{label}.timeout")),
    )


def _auth_settings(value: Any, label: str) -> AuthSettings:
    data = _mapping(value, label)
    if any(key in data for key in ("token", "password", "username")):
        raise ConfigError(f"{label} must reference *_env variables, not credentials")
    return AuthSettings(
        bearer_token_env=(_required_string(data["bearer_token_env"], f"{label}.bearer_token_env")
                          if data.get("bearer_token_env") is not None else None),
        basic_username_env=(_required_string(data["basic_username_env"], f"{label}.basic_username_env")
                            if data.get("basic_username_env") is not None else None),
        basic_password_env=(_required_string(data["basic_password_env"], f"{label}.basic_password_env")
                            if data.get("basic_password_env") is not None else None),
    )


def _target_url(data: Mapping[str, Any], label: str, default_host: str) -> str:
    """Build an endpoint URL from the device host, protocol, port and path.

    The legacy ``url`` form remains accepted for compatibility, but its host
    is replaced with the configured device host so endpoint checks cannot
    silently target a different machine.
    """

    legacy_url = data.get("url")
    if legacy_url is not None:
        parsed = urlsplit(_required_string(legacy_url, f"{label}.url"))
        protocol = parsed.scheme.lower()
        if protocol not in {"http", "https"} or not parsed.hostname:
            raise ConfigError(f"{label}.url must use http or https and include a host")
        port = parsed.port or (443 if protocol == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
    else:
        protocol = _required_string(data.get("protocol"), f"{label}.protocol").lower()
        if protocol not in {"http", "https"}:
            raise ConfigError(f"{label}.protocol must be http or https")
        port = int(_number(data.get("port", 443 if protocol == "https" else 80),
                           f"{label}.port", integer=True))
        path = str(data.get("path", "/"))
        if not path.startswith("/"):
            raise ConfigError(f"{label}.path must start with '/'")

    if not 1 <= port <= 65535:
        raise ConfigError(f"{label}.port must be between 1 and 65535")
    host = default_host
    authority_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return urlunsplit((protocol, f"{authority_host}:{port}", path, "", ""))


def _endpoint(value: Any, label: str, default_timeout: float, verify_tls: bool, default_host: str) -> HTTPEndpoint:
    data = _mapping(value, label)
    name = _required_string(data.get("name"), f"{label}.name")
    url = _target_url(data, label, default_host)
    statuses = data.get("expected_status", [200])
    statuses = [statuses] if isinstance(statuses, int) else _as_list(statuses, f"{label}.expected_status")
    expected_status = tuple(int(status) for status in statuses)
    if not expected_status or any(status < 100 or status > 599 for status in expected_status):
        raise ConfigError(f"{label}.expected_status must contain HTTP status codes")
    headers = _mapping(data.get("headers"), f"{label}.headers")
    if any(key.lower() in {"authorization", "cookie"} for key in headers):
        raise ConfigError(f"{label}.headers must not contain credentials")
    return HTTPEndpoint(
        name=name,
        url=url,
        expected_status=expected_status,
        timeout=float(_number(data.get("timeout", default_timeout), f"{label}.timeout")),
        verify_tls=_bool(data.get("verify_tls"), f"{label}.verify_tls", verify_tls),
        follow_redirects=_bool(data.get("follow_redirects"), f"{label}.follow_redirects", True),
        content_type=data.get("content_type"),
        contains=tuple(str(item) for item in _as_list(data.get("contains"), f"{label}.contains")),
        headers={str(key): str(val) for key, val in headers.items()},
        auth=_auth_settings(data.get("auth"), f"{label}.auth"),
        json_fields=_mapping(data.get("json_fields"), f"{label}.json_fields"),
        required=_bool(data.get("required"), f"{label}.required", True),
    )


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> AcceptanceConfig:
    """Load and validate a configuration file, then apply documented env overrides."""

    environ = os.environ if environ is None else environ
    if not path.is_file():
        raise ConfigError(f"configuration file does not exist: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    root = _mapping(raw, "configuration")
    device = _mapping(root.get("device"), "device")
    ssh_data = _mapping(device.get("ssh", root.get("ssh")), "device.ssh")

    host = environ.get("RPI4_HOST", device.get("host"))
    username = environ.get("RPI4_SSH_USER", ssh_data.get("username", device.get("username")))
    key_path = environ.get("RPI4_SSH_KEY", ssh_data.get("key_path"))
    password_env = ssh_data.get("password_env")
    allow_empty_password = _bool(
        environ.get("RPI4_ALLOW_EMPTY_PASSWORD", ssh_data.get("allow_empty_password")),
        "device.ssh.allow_empty_password",
        False,
    )
    known_hosts = environ.get("RPI4_KNOWN_HOSTS", ssh_data.get("known_hosts", "~/.ssh/known_hosts"))
    if not host:
        raise ConfigError("device.host (or RPI4_HOST) is required")
    if not username:
        raise ConfigError("device.ssh.username (or RPI4_SSH_USER) is required")
    if not key_path and not password_env and not allow_empty_password:
        raise ConfigError("configure device.ssh.key_path, password_env or explicit allow_empty_password")

    connect_timeout = float(_number(environ.get("RPI4_SSH_TIMEOUT", ssh_data.get("connect_timeout", 10)), "connect_timeout"))
    command_timeout = float(_number(environ.get("RPI4_COMMAND_TIMEOUT", ssh_data.get("command_timeout", 30)), "command_timeout"))
    ssh = SSHSettings(
        host=str(host),
        port=int(_number(environ.get("RPI4_SSH_PORT", ssh_data.get("port", 22)), "ssh.port", integer=True)),
        username=str(username),
        key_path=Path(str(key_path)).expanduser() if key_path else None,
        known_hosts=Path(str(known_hosts)).expanduser(),
        connect_timeout=connect_timeout,
        command_timeout=command_timeout,
        password_env=_required_string(password_env, "device.ssh.password_env") if password_env else None,
        allow_empty_password=allow_empty_password,
    )
    if not 1 <= ssh.port <= 65535:
        raise ConfigError("device.ssh.port must be between 1 and 65535")

    connectivity = _mapping(root.get("connectivity"), "connectivity")
    tcp_ports = tuple(
        _port_check(item, f"connectivity.tcp_ports[{index}]", ssh.host, connect_timeout)
        for index, item in enumerate(_as_list(connectivity.get("tcp_ports"), "connectivity.tcp_ports"))
    )

    system_data = _mapping(root.get("system"), "system")
    filesystems = tuple(
        FilesystemCheck(
            path=_required_string(_mapping(item, f"system.filesystems[{index}]").get("path"), f"system.filesystems[{index}].path"),
            min_free_mb=int(_number(_mapping(item, f"system.filesystems[{index}]").get("min_free_mb", 0), f"system.filesystems[{index}].min_free_mb", integer=True)),
        )
        for index, item in enumerate(_as_list(system_data.get("filesystems"), "system.filesystems"))
    )
    system = SystemSettings(
        required_commands=tuple(str(item) for item in _as_list(system_data.get("required_commands"), "system.required_commands")),
        filesystems=filesystems,
        min_available_memory_mb=(int(_number(system_data["min_available_memory_mb"], "system.min_available_memory_mb", integer=True)) if system_data.get("min_available_memory_mb") is not None else None),
        max_memory_used_percent=(float(_number(system_data["max_memory_used_percent"], "system.max_memory_used_percent")) if system_data.get("max_memory_used_percent") is not None else None),
        min_uptime_seconds=(int(_number(system_data["min_uptime_seconds"], "system.min_uptime_seconds", integer=True)) if system_data.get("min_uptime_seconds") is not None else None),
        critical_log_patterns=tuple(str(item) for item in _as_list(system_data.get("critical_log_patterns"), "system.critical_log_patterns")),
        log_source=system_data.get("log_source"),
    )

    services: list[ServiceCheck] = []
    for index, item in enumerate(_as_list(root.get("services"), "services")):
        data = _mapping(item, f"services[{index}]")
        name = _required_string(data.get("name"), f"services[{index}].name")
        ports = tuple(
            _port_check(port, f"services[{index}].ports[{port_index}]", ssh.host, connect_timeout)
            for port_index, port in enumerate(_as_list(data.get("ports"), f"services[{index}].ports"))
        )
        services.append(ServiceCheck(
            name=name,
            manager=str(data.get("manager", "systemd")),
            expected_state=str(data.get("expected_state", "active")),
            ports=ports,
            diagnostics=_bool(data.get("diagnostics"), f"services[{index}].diagnostics", False),
            required=_bool(data.get("required"), f"services[{index}].required", True),
        ))

    http_data = _mapping(root.get("http"), "http")
    http_headers = _mapping(http_data.get("headers"), "http.headers")
    if any(key.lower() in {"authorization", "cookie"} for key in http_headers):
        raise ConfigError("http.headers must not contain credentials")
    header_env = _mapping(http_data.get("headers_from_env"), "http.headers_from_env")
    http_timeout = float(_number(http_data.get("timeout", 10), "http.timeout"))
    http_verify = _bool(http_data.get("verify_tls"), "http.verify_tls", True)
    http = HTTPSettings(
        headers={str(key): str(val) for key, val in http_headers.items()},
        headers_from_env={str(key): _required_string(val, f"http.headers_from_env.{key}") for key, val in header_env.items()},
        endpoints=tuple(_endpoint(item, f"http.endpoints[{index}]", http_timeout, http_verify, ssh.host)
                        for index, item in enumerate(_as_list(http_data.get("endpoints"), "http.endpoints"))),
    )

    mpd_data = _mapping(root.get("mpd"), "mpd")
    mpd = MPDSettings(
        startup_timeout=float(_number(mpd_data.get("startup_timeout", 8), "mpd.startup_timeout")),
        poll_interval=float(_number(mpd_data.get("poll_interval", 0.5), "mpd.poll_interval")),
    )

    snapcast_data = _mapping(root.get("snapcast"), "snapcast")
    snapcast_http_port = int(_number(snapcast_data.get("http_port", 1780), "snapcast.http_port", integer=True))
    if not 1 <= snapcast_http_port <= 65535:
        raise ConfigError("snapcast.http_port must be between 1 and 65535")
    snapcast = SnapcastSettings(
        http_port=snapcast_http_port,
        expected_stream=_required_string(snapcast_data.get("expected_stream", "MPD"), "snapcast.expected_stream"),
        expected_source_path=_required_string(
            snapcast_data.get("expected_source_path", "/tmp/snapmpdfifo"),
            "snapcast.expected_source_path",
        ),
        bluetooth_stream=_required_string(
            snapcast_data.get("bluetooth_stream", "Bluetooth"),
            "snapcast.bluetooth_stream",
        ),
    )

    pulseaudio_data = _mapping(root.get("pulseaudio"), "pulseaudio")
    pulseaudio_port = int(_number(pulseaudio_data.get("tcp_port", 4713), "pulseaudio.tcp_port", integer=True))
    if not 1 <= pulseaudio_port <= 65535:
        raise ConfigError("pulseaudio.tcp_port must be between 1 and 65535")
    pulseaudio = PulseAudioSettings(
        tcp_port=pulseaudio_port,
        expected_protocol_version=_required_string(
            pulseaudio_data.get("expected_protocol_version", "35"),
            "pulseaudio.expected_protocol_version",
        ),
        expected_user_name=_required_string(
            pulseaudio_data.get("expected_user_name", "pulse"),
            "pulseaudio.expected_user_name",
        ),
        expected_server_name=_required_string(
            pulseaudio_data.get("expected_server_name", "pulseaudio"),
            "pulseaudio.expected_server_name",
        ),
        expected_sample_specification=_required_string(
            pulseaudio_data.get("expected_sample_specification", "s16le 2ch 44100Hz"),
            "pulseaudio.expected_sample_specification",
        ),
        expected_default_sink=_required_string(
            pulseaudio_data.get("expected_default_sink", "alsa_output.headphones.stereo-fallback"),
            "pulseaudio.expected_default_sink",
        ),
        expected_default_source=_required_string(
            pulseaudio_data.get("expected_default_source", "alsa_output.headphones.stereo-fallback.monitor"),
            "pulseaudio.expected_default_source",
        ),
    )

    web_data = _mapping(root.get("web_ui"), "web_ui")
    web_endpoints: list[WebUIEndpoint] = []
    for index, item in enumerate(_as_list(web_data.get("endpoints"), "web_ui.endpoints")):
        data = _mapping(item, f"web_ui.endpoints[{index}]")
        web_endpoints.append(WebUIEndpoint(
            name=_required_string(data.get("name"), f"web_ui.endpoints[{index}].name"),
            url=_target_url(data, f"web_ui.endpoints[{index}]", ssh.host),
            selectors=tuple(str(selector) for selector in _as_list(data.get("selectors"), f"web_ui.endpoints[{index}].selectors")),
            interactions=tuple(_mapping(action, f"web_ui.endpoints[{index}].interactions") for action in _as_list(data.get("interactions"), f"web_ui.endpoints[{index}].interactions")),
            screenshot_on_failure=_bool(data.get("screenshot_on_failure"), f"web_ui.endpoints[{index}].screenshot_on_failure", True),
            screenshot_on_success=_bool(data.get("screenshot_on_success"), f"web_ui.endpoints[{index}].screenshot_on_success", False),
            ignore_console_errors=tuple(
                str(pattern)
                for pattern in _as_list(
                    data.get("ignore_console_errors"),
                    f"web_ui.endpoints[{index}].ignore_console_errors",
                )
            ),
        ))
    web_ui = WebUISettings(
        enabled=_bool(web_data.get("enabled"), "web_ui.enabled", False),
        headless=_bool(web_data.get("headless"), "web_ui.headless", True),
        fail_on_console_errors=_bool(web_data.get("fail_on_console_errors"), "web_ui.fail_on_console_errors", True),
        screenshot_dir=Path(str(web_data.get("screenshot_dir", "reports/screenshots"))).expanduser(),
        endpoints=tuple(web_endpoints),
    )

    return AcceptanceConfig(
        name=str(environ.get("RPI4_DEVICE_NAME", device.get("name", host))),
        ssh=ssh,
        tcp_ports=tcp_ports,
        system=system,
        services=tuple(services),
        http=http,
        mpd=mpd,
        snapcast=snapcast,
        pulseaudio=pulseaudio,
        web_ui=web_ui,
    )


def redact(value: str) -> str:
    """Remove common credential-shaped values before they reach a report."""

    for pattern in (
        r"(?i)(authorization\s*[:=]\s*)(?:(?:bearer|basic)\s+)?([^\s,;]+)",
        r"(?i)(bearer\s+)([^\s,;]+)",
        r"(?i)((?:password|passwd|token|secret|private[_ -]?key)\s*[:=]\s*)([^\s,;]+)",
    ):
        value = re.sub(pattern, r"\1[REDACTED]", value)
    return value


def safe_url(value: str) -> str:
    """Return an endpoint URL without query strings or embedded credentials."""

    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    host = hostname
    if parsed.port is not None:
        host = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path or "/", "", ""))


class SSHConnection:
    """Small, testable wrapper around Paramiko with safe argv execution."""

    def __init__(self, settings: SSHSettings):
        self.settings = settings
        self.client: paramiko.SSHClient | None = None

    def connect(self) -> None:
        if self.settings.key_path is not None and not self.settings.key_path.is_file():
            raise SSHError(f"private key does not exist: {self.settings.key_path}")
        if not self.settings.known_hosts.is_file():
            raise SSHHostKeyError(f"known_hosts does not exist: {self.settings.known_hosts}")
        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys(str(self.settings.known_hosts))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            connect_options: dict[str, Any] = {
                "hostname": self.settings.host,
                "port": self.settings.port,
                "username": self.settings.username,
                "timeout": self.settings.connect_timeout,
                "banner_timeout": self.settings.connect_timeout,
                "auth_timeout": self.settings.connect_timeout,
                "allow_agent": False,
                "look_for_keys": False,
            }
            if self.settings.key_path is not None:
                connect_options["key_filename"] = str(self.settings.key_path)
            if self.settings.password_env:
                password = os.environ.get(self.settings.password_env)
                if password is None:
                    raise SSHError(f"environment variable {self.settings.password_env} is required for SSH")
                connect_options["password"] = password
            elif self.settings.allow_empty_password:
                connect_options["password"] = ""
            client.connect(
                **connect_options,
            )
        except paramiko.AuthenticationException as exc:
            client.close()
            raise SSHAuthenticationError(f"SSH authentication failed for {self.settings.username}@{self.settings.host}") from exc
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise SSHHostKeyError(f"SSH host key rejected for {self.settings.host}") from exc
        except (NoValidConnectionsError, socket.timeout, TimeoutError, OSError) as exc:
            client.close()
            raise SSHUnavailableError(f"SSH host is unavailable: {self.settings.host}:{self.settings.port}") from exc
        except paramiko.SSHException as exc:
            client.close()
            raise SSHError(f"SSH connection failed for {self.settings.host}: {exc}") from exc
        self.client = client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def run(self, argv: Sequence[str], timeout: float | None = None) -> CommandResult:
        """Run an argv-like command with shell-quoted arguments."""

        if not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("argv must contain at least one string")
        if self.client is None:
            raise SSHError("SSH connection is not open")
        command = " ".join(shlex.quote(item) for item in argv)
        _, stdout, _stderr = self.client.exec_command(command)
        channel = stdout.channel
        timeout = self.settings.command_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        output = bytearray()
        errors = bytearray()
        while not channel.exit_status_ready():
            if channel.recv_ready():
                output.extend(channel.recv(65536))
            if channel.recv_stderr_ready():
                errors.extend(channel.recv_stderr(65536))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                channel.close()
                raise SSHCommandTimeout(f"remote command timed out after {timeout:.1f}s: {argv[0]}")
            select.select([channel], [], [], min(0.1, remaining))
        while channel.recv_ready():
            output.extend(channel.recv(65536))
        while channel.recv_stderr_ready():
            errors.extend(channel.recv_stderr(65536))
        return CommandResult(channel.recv_exit_status(), output.decode("utf-8", errors="replace"), errors.decode("utf-8", errors="replace"))


def _http_headers(config: AcceptanceConfig) -> dict[str, str]:
    headers = dict(config.http.headers)
    for header, env_name in config.http.headers_from_env.items():
        if env_name in os.environ:
            headers[header] = os.environ[env_name]
    return headers


def request_endpoint(session: requests.Session, config: AcceptanceConfig, endpoint: HTTPEndpoint) -> requests.Response:
    """Perform one configured request without exposing auth values to callers."""

    auth = endpoint.auth
    request_auth = None
    headers = _http_headers(config)
    if auth.bearer_token_env:
        token = os.environ.get(auth.bearer_token_env)
        if not token:
            raise ConfigError(f"environment variable {auth.bearer_token_env} is required for endpoint {endpoint.name}")
        headers["Authorization"] = f"Bearer {token}"
    elif auth.basic_username_env or auth.basic_password_env:
        if not auth.basic_username_env or not auth.basic_password_env:
            raise ConfigError(f"endpoint {endpoint.name} requires both basic auth environment variables")
        username = os.environ.get(
            auth.basic_username_env,
            _TRANSMISSION_TEST_DEFAULTS.get(auth.basic_username_env),
        )
        password = os.environ.get(
            auth.basic_password_env,
            _TRANSMISSION_TEST_DEFAULTS.get(auth.basic_password_env),
        )
        if username is None or password is None:
            raise ConfigError(f"basic auth environment variables are missing for endpoint {endpoint.name}")
        request_auth = HTTPBasicAuth(username, password)
    return session.get(endpoint.url, headers=headers, auth=request_auth, timeout=endpoint.timeout,
                       verify=endpoint.verify_tls, allow_redirects=endpoint.follow_redirects)


def json_field(value: Any, path: str) -> Any:
    """Read a dotted JSON field path, including list indexes."""

    current = value
    for part in path.split("."):
        if isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        elif isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            raise KeyError(path)
    return current


def parse_memory_kib(text: str) -> tuple[int, int]:
    """Return available and total memory in KiB from /proc/meminfo."""

    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2))
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        raise ValueError("/proc/meminfo does not contain MemTotal and MemAvailable")
    return available, total


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("rpi4 acceptance")
    try:
        group.addoption("--config", action="store", default=None,
                        help="YAML device configuration (default: config/test.yaml or ACCEPTANCE_CONFIG)")
    except ValueError as exc:
        # The acceptance root shim loads this module early so that --config is
        # available when pytest is invoked without an explicit test path. The
        # same module is then discovered as tests/conftest.py; pytest calls
        # this hook a second time in that case.
        if "--config" not in str(exc):
            raise


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "acceptance: test requiring a configured Raspberry Pi 4")
    config.addinivalue_line("markers", "unit: host-only framework unit test")
    Path("reports").mkdir(parents=True, exist_ok=True)
    metadata = getattr(config, "_metadata", None)
    if metadata is not None:
        metadata["test framework"] = "Raspberry Pi 4 acceptance tests"
        metadata["test started"] = datetime.now(timezone.utc).isoformat()
        metadata["device"] = os.environ.get("RPI4_DEVICE_NAME", "configured Raspberry Pi 4")
        metadata["target build-info"] = _TARGET_BUILD_INFO


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> None:
    """Put a description of every check into console, file and HTML logs."""

    # The acceptance root shim and tests/conftest.py can both register this
    # module. Keep the description readable if pytest invokes the hook twice.
    if getattr(item, "_acceptance_description_logged", False):
        return
    item._acceptance_description_logged = True
    description = _test_description(item)
    logging.getLogger("acceptance").info("TEST CHECK: %s — %s", item.nodeid, description)
    item.user_properties.append(("checked", description))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or getattr(item, "_acceptance_result_recorded", False):
        return
    item._acceptance_result_recorded = True
    description = _test_description(item)
    report.acceptance_description = description
    item.user_properties.append(("result", report.outcome))
    item.user_properties.append(("duration_ms", f"{report.duration * 1000:.0f}"))
    report.sections.append((
        "Acceptance check",
        f"TEST CHECK: {item.nodeid} — {description}\n"
        f"TEST RESULT: {report.outcome} duration={report.duration * 1000:.0f}ms",
    ))


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Write the final outcome to the persistent pytest log as well."""

    if report.when != "call" or getattr(report, "_acceptance_result_logged", False):
        return
    report._acceptance_result_logged = True
    logging.getLogger("acceptance").info(
        "TEST RESULT: %s — %s duration=%.0fms",
        report.nodeid,
        report.outcome,
        report.duration * 1000,
    )


@pytest.hookimpl(optionalhook=True)
def pytest_html_results_table_header(cells) -> None:  # pragma: no cover - optional plugin callback
    global _HTML_HEADER_ADDED
    if _HTML_HEADER_ADDED:
        return
    _HTML_HEADER_ADDED = True
    cells.insert(2, "<th class='sortable'>Checked</th>")


@pytest.hookimpl(optionalhook=True)
def pytest_html_results_table_row(report, cells) -> None:  # pragma: no cover - optional plugin callback
    if getattr(report, "_acceptance_row_added", False):
        return
    report._acceptance_row_added = True
    description = escape(getattr(report, "acceptance_description", ""))
    cells.insert(2, f"<td>{description}</td>")


@pytest.fixture(scope="session")
def test_config(pytestconfig: pytest.Config) -> AcceptanceConfig:
    configured = pytestconfig.getoption("--config") or os.environ.get("ACCEPTANCE_CONFIG")
    path = Path(configured).expanduser() if configured else Path("config/test.yaml")
    if not path.is_file() and not configured:
        pytest.skip(f"no device configuration found at {path}; copy config/test.example.yaml to config/test.yaml")
    try:
        return load_config(path)
    except ConfigError as exc:
        pytest.fail(f"invalid acceptance configuration: {exc}")


@pytest.fixture(scope="session")
def ssh_client(test_config: AcceptanceConfig, pytestconfig: pytest.Config) -> Iterator[SSHConnection]:
    global _TARGET_BUILD_INFO
    connection = SSHConnection(test_config.ssh)
    try:
        connection.connect()
    except SSHError as exc:
        pytest.skip(f"SSH-dependent check skipped: {redact(str(exc))}")
    try:
        build_info = connection.run(["cat", "/etc/mediaserver/build-info"])
    except SSHError as exc:
        _TARGET_BUILD_INFO = f"unavailable ({redact(str(exc))})"
    else:
        if build_info.returncode == 0 and build_info.stdout.strip():
            _TARGET_BUILD_INFO = redact(build_info.stdout.strip())
        else:
            _TARGET_BUILD_INFO = "unavailable (missing /etc/mediaserver/build-info)"
            logging.getLogger("acceptance").warning(
                "TARGET BUILD-INFO unavailable: %s",
                redact(build_info.stderr.strip() or build_info.stdout.strip()),
            )
    logging.getLogger("acceptance").info("TARGET BUILD-INFO: %s", _TARGET_BUILD_INFO)
    metadata = getattr(pytestconfig, "_metadata", None)
    if metadata is not None:
        metadata["target build-info"] = _TARGET_BUILD_INFO
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def http_session() -> Iterator[requests.Session]:
    session = requests.Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def socket_connector():
    return socket.create_connection


def pytest_html_results_summary(prefix, summary, postfix) -> None:  # pragma: no cover - optional plugin callback
    prefix.extend([
        "<p>Test runner: Ubuntu host; target: Raspberry Pi 4.</p>",
        f"<p>Target build-info: {escape(_TARGET_BUILD_INFO)}</p>",
    ])

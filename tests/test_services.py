"""Configured service-manager and service-port checks."""

from __future__ import annotations

import socket
import logging
from textwrap import indent

import pytest

from conftest import AcceptanceConfig, ServiceCheck, SSHConnection, redact


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def _service_state(ssh_client: SSHConnection, service: ServiceCheck) -> tuple[str, str]:
    if service.manager == "systemd":
        result = ssh_client.run(["systemctl", "is-active", service.name])
        state = result.stdout.strip() or result.stderr.strip() or "unknown"
        return state, result.stderr.strip()
    if service.manager == "sysv":
        result = ssh_client.run(["service", service.name, "status"])
        return ("active" if result.returncode == 0 else "inactive"), result.stderr.strip()
    if service.manager == "process":
        result = ssh_client.run(["pgrep", "-x", service.name])
        return ("active" if result.returncode == 0 else "inactive"), result.stderr.strip()
    pytest.fail(f"unsupported service manager in configuration: {service.manager}")


def _diagnostics(ssh_client: SSHConnection, service: ServiceCheck) -> str:
    if not service.diagnostics:
        return ""
    output: list[str] = []
    if service.manager == "systemd":
        commands = (
            ("systemd status", ["systemctl", "status", "--no-pager", "--lines=10", service.name]),
            ("journal", ["journalctl", "-u", service.name, "-n", "30", "--no-pager"]),
        )
        for label, argv in commands:
            result = ssh_client.run(argv)
            details = redact(result.stdout + result.stderr).strip() or "(no output)"
            output.append(f"{label}:\n{indent(details[-4000:], '  ')}")
    ps = ssh_client.run(["ps", "-eo", "pid,comm,args", "--no-headers"])
    output.append(f"processes:\n{indent(redact(ps.stdout).strip()[-2000:], '  ')}")
    return "\n\nDiagnostics (output truncated):\n" + "\n\n".join(output)


def _service_failure(service: ServiceCheck, state: str, diagnostics: str) -> str:
    return (
        "service check failed\n"
        f"  service: {service.name}\n"
        f"  manager: {service.manager}\n"
        f"  expected state: {service.expected_state}\n"
        f"  actual state: {state}"
        f"{diagnostics}"
    )


def test_configured_service_states(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> None:
    if not test_config.services:
        pytest.skip("no services configured")
    for service in test_config.services:
        state, error = _service_state(ssh_client, service)
        LOGGER.info("Service state result: %s manager=%s state=%s expected=%s",
                    service.name, service.manager, state, service.expected_state)
        if state == "unknown" and "not found" in error.lower():
            if service.required:
                pytest.fail(f"configured service manager is not available for required service {service.name}")
            pytest.skip(f"optional service manager is not available for {service.name}")
        if service.expected_state == "active":
            if state != "active" and not service.required:
                pytest.skip(f"optional service {service.name} is {state}")
            assert state == "active", _service_failure(
                service, state, _diagnostics(ssh_client, service)
            )
        else:
            if state != service.expected_state and not service.required:
                pytest.skip(f"optional service {service.name} is {state}")
            assert state == service.expected_state, _service_failure(
                service, state, _diagnostics(ssh_client, service)
            )


def test_configured_service_ports(test_config: AcceptanceConfig, socket_connector) -> None:
    checks = [port for service in test_config.services for port in service.ports]
    if not checks:
        pytest.skip("no service ports configured")
    for check in checks:
        try:
            with socket_connector((check.host, check.port), timeout=check.timeout):
                LOGGER.info("Service port result: %s %s:%s reachable", check.name, check.host, check.port)
        except socket.timeout:
            pytest.fail(f"service port {check.name} timed out after {check.timeout:.1f}s")
        except OSError as exc:
            pytest.fail(f"service port {check.name} is unavailable: {exc}")

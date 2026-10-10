"""Host-side checks of the Raspberry Pi operating system."""

from __future__ import annotations

import shlex
import logging

import pytest

from conftest import AcceptanceConfig, SSHConnection, parse_memory_kib, redact


pytestmark = pytest.mark.acceptance
LOGGER = logging.getLogger(__name__)


def _run_or_skip(ssh_client: SSHConnection, argv: list[str], description: str):
    result = ssh_client.run(argv)
    if result.returncode != 0:
        pytest.skip(f"{description} is unavailable on the target: {redact(result.stderr.strip())}")
    return result


def test_system_information(ssh_client: SSHConnection) -> None:
    uname = _run_or_skip(ssh_client, ["uname", "-a"], "uname")
    LOGGER.info("System information result: uname=%s", uname.stdout.strip())
    assert uname.stdout.strip(), "uname returned no system information"


def test_required_diagnostic_commands(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> None:
    if not test_config.system.required_commands:
        pytest.skip("no diagnostic commands configured")
    for command in test_config.system.required_commands:
        result = ssh_client.run(["sh", "-c", f"command -v {shlex.quote(command)}"])
        if result.returncode != 0:
            pytest.skip(f"diagnostic command is not available (reported separately): {command}")
        LOGGER.info("Diagnostic command result: %s available at %s", command, result.stdout.strip())


def test_configured_filesystems(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> None:
    if not test_config.system.filesystems:
        pytest.skip("no filesystem thresholds configured")
    for check in test_config.system.filesystems:
        result = ssh_client.run(["df", "-Pk", check.path])
        if result.returncode != 0:
            pytest.skip(f"df is unavailable for configured filesystem {check.path}")
        rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if len(rows) < 2 or len(rows[-1]) < 4:
            pytest.fail(f"could not parse df output for {check.path}")
        try:
            free_kb = int(rows[-1][-3])
        except ValueError as exc:
            pytest.fail(f"could not parse available space for {check.path}")
            raise exc
        free_mb = free_kb / 1024
        LOGGER.info("Filesystem result: path=%s free=%.0fMiB minimum=%dMiB",
                    check.path, free_mb, check.min_free_mb)
        assert free_mb >= check.min_free_mb, (
            f"filesystem {check.path} has {free_mb:.0f} MiB free; "
            f"minimum is {check.min_free_mb} MiB"
        )


def test_memory_thresholds(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> None:
    if test_config.system.min_available_memory_mb is None and test_config.system.max_memory_used_percent is None:
        pytest.skip("no memory thresholds configured")
    result = ssh_client.run(["cat", "/proc/meminfo"])
    if result.returncode != 0:
        pytest.skip("/proc/meminfo is unavailable; memory check cannot be performed")
    try:
        available_kib, total_kib = parse_memory_kib(result.stdout)
    except ValueError as exc:
        pytest.skip(str(exc))
    available_mb = available_kib / 1024
    used_percent = 100 * (1 - available_kib / total_kib)
    LOGGER.info("Memory result: available=%.0fMiB used=%.1f%% thresholds=(min_available=%sMiB, max_used=%s%%)",
                available_mb, used_percent, test_config.system.min_available_memory_mb,
                test_config.system.max_memory_used_percent)
    if test_config.system.min_available_memory_mb is not None:
        assert available_mb >= test_config.system.min_available_memory_mb, (
            f"only {available_mb:.0f} MiB RAM is available; "
            f"minimum is {test_config.system.min_available_memory_mb} MiB"
        )
    if test_config.system.max_memory_used_percent is not None:
        assert used_percent <= test_config.system.max_memory_used_percent, (
            f"RAM usage is {used_percent:.1f}%; maximum is "
            f"{test_config.system.max_memory_used_percent:.1f}%"
        )


def test_uptime_threshold(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> None:
    if test_config.system.min_uptime_seconds is None:
        pytest.skip("uptime threshold is not configured")
    result = ssh_client.run(["cat", "/proc/uptime"])
    if result.returncode != 0:
        pytest.skip("/proc/uptime is unavailable")
    try:
        uptime = float(result.stdout.split()[0])
    except (IndexError, ValueError):
        pytest.fail("could not parse /proc/uptime")
    LOGGER.info("Uptime result: uptime=%.0fs minimum=%ss", uptime, test_config.system.min_uptime_seconds)
    assert uptime >= test_config.system.min_uptime_seconds, (
        f"uptime is {uptime:.0f}s; minimum is {test_config.system.min_uptime_seconds}s"
    )


def test_critical_log_patterns(test_config: AcceptanceConfig, ssh_client: SSHConnection) -> None:
    settings = test_config.system
    if not settings.critical_log_patterns or not settings.log_source:
        pytest.skip("critical log check is not configured")
    result = ssh_client.run(["tail", "-n", "200", settings.log_source])
    if result.returncode != 0:
        pytest.skip("configured log source is unavailable")
    log_text = redact(result.stdout)
    found = [pattern for pattern in settings.critical_log_patterns if pattern in log_text]
    LOGGER.info("Critical log result: source=%s patterns_checked=%d matches=%d",
                settings.log_source, len(settings.critical_log_patterns), len(found))
    assert not found, f"configured critical log patterns were found: {', '.join(found)}"

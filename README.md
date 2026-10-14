# Raspberry Pi 4 acceptance tests

This is a host-side acceptance-test framework for the `meta-mediaserver`
Yocto layer. Tests run on an Ubuntu test computer and inspect a Raspberry Pi 4
over SSH, TCP and HTTP/HTTPS. Python, pytest and all test libraries are
installed only in the Ubuntu environment; nothing is added to the Yocto image
or installed on the Raspberry Pi 4.

The framework is intentionally independent of a particular image. Configure
the services and endpoints that exist in the image being tested.

## Requirements

- Ubuntu test computer with Python 3 and network access to the Raspberry Pi 4.
- A booted Raspberry Pi 4 running the image under test.
- An SSH server on the device and a test user authorized with a private key.
- `known_hosts` containing the verified Raspberry Pi 4 host key.
- Optional: Chromium dependencies if browser checks are enabled.

The device does not need Python, pytest, Paramiko, requests, Playwright or any
other framework dependency.

## First setup

Run the following commands from `meta-mediaserver/tests/acceptance/` on the
Ubuntu test computer:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp config/test.example.yaml config/test.yaml
```

Edit `config/test.yaml`. At minimum set `device.host`, the SSH username and
either the path to a private key or an explicitly enabled development-only
empty-password mode. The local configuration is ignored by Git.

For the first supported application profile use:

```bash
cp config/mediaserver-image-base.example.yaml config/test.yaml
```

This profile targets `mediaserver-image-base` and intentionally excludes
Tvheadend and Nextcloud. Services marked `required: false` are reported as
skipped when the selected hardware or configuration does not provide them.
For the supplied development image, `allow_empty_password: true` is used
explicitly; do not use that setting on a production device.

The `mpd` section configures the functional MPD/MPC playback check. It reads
all tracks from the target's MPD database with `mpc listall`, adds them to a
temporary playlist, starts the first track and verifies that elapsed time
advances.
The test temporarily replaces the playlist and restores the previous playlist
afterwards. An MPD connection or a successful `mpc play` command alone is not
enough for this check to pass; audio-output errors and a paused/stopped state
are failures.

The `snapcast` section configures the MPD routing check. The test reads
`default_source` or the source order from `/etc/snapserver.conf`, queries
Snapserver's HTTP JSON-RPC endpoint, verifies that the MPD stream uses
`/tmp/snapmpdfifo`, and checks
that the connected Snapclient on the Raspberry Pi is assigned to `MPD`, not
the `Bluetooth` stream.

The `pulseaudio` section configures the TCP PulseAudio check. It runs
`pactl -s tcp:<device>:4713 info` on the Ubuntu test computer and verifies the
remote server, protocol version, user, sample specification and default sink
and source. Install `pactl` with `sudo apt install pulseaudio-utils` if it is
not present.

Before the first test, verify the Raspberry Pi 4 host fingerprint through a
trusted channel and then add that host key to the Ubuntu user's
`~/.ssh/known_hosts`. The framework uses Paramiko's rejecting host-key policy:
it never accepts an unknown key automatically.

Example key setup (the public key must be installed on the device by the
administrator or through an already trusted SSH session):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/rpi4_acceptance_ed25519
ssh-keygen -F 192.0.2.10
```

Do not commit the private key, passwords, tokens or the local configuration.
For authenticated HTTP endpoints use environment variables, for example
`RPI4_API_TOKEN`, referenced by `bearer_token_env` in the YAML.
The Transmission Web UI check uses the development credentials
`transmission`/`transmission` by default. Explicit environment values override
these defaults:

```bash
export TRANSMISSION_RPC_USERNAME=transmission
export TRANSMISSION_RPC_PASSWORD='your-transmission-password'
```

The credentials are never stored in the YAML or written to the test log.

## Running tests

All commands below assume the current directory is
`meta-mediaserver/tests/acceptance/` and the virtual environment is active.

```bash
pytest --config config/test.yaml
pytest --config config/test.yaml tests/test_connectivity.py
pytest --config config/test.yaml tests/test_system.py
pytest --config config/test.yaml tests/test_services.py
pytest --config config/test.yaml tests/test_http.py
pytest --config config/test.yaml tests/test_base_applications.py
pytest --config config/test.yaml tests/test_snapcast.py
pytest --config config/test.yaml tests/test_web_ui.py
```

The unit tests do not need a physical device:

```bash
pytest tests/test_helpers.py
```

The configuration can instead be selected with `ACCEPTANCE_CONFIG`. The
following environment variables override the corresponding SSH settings:
`RPI4_HOST`, `RPI4_SSH_PORT`, `RPI4_SSH_USER`, `RPI4_SSH_KEY`,
`RPI4_KNOWN_HOSTS`, `RPI4_SSH_TIMEOUT`, `RPI4_COMMAND_TIMEOUT` and
`RPI4_DEVICE_NAME`.

## HTML reports

`pytest-html` is part of the base requirements. Reports and screenshots are
local artifacts ignored by Git:

```bash
mkdir -p reports
pytest --config config/test.yaml --html=reports/rpi4-acceptance.html --self-contained-html
```

Every test writes a `TEST CHECK` entry and a `TEST RESULT` entry at `INFO`
level to the console and to `reports/pytest.log`. The HTML report also shows
the properties `checked`, `result` and `duration_ms` for every test. HTTP
checks additionally record each endpoint name, sanitized URL, returned
status, expected statuses and elapsed time.

The report contains the configured device name, target `/etc/mediaserver/build-info`,
start time, test result and failure details. Request credentials and common
secret-shaped diagnostic values are redacted; do not configure sensitive
response fragments or place secrets in endpoint URLs.

## Optional browser checks

Install the optional dependency only when browser testing is needed:

```bash
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
```

Set `web_ui.enabled: true` and configure selectors/interactions. The browser
is headless, JavaScript errors can fail the check, and a screenshot is saved
under `reports/screenshots/` after a failed endpoint check. The browser and
context are closed even when a check fails.

## What is checked

System checks run commands on the Raspberry Pi 4 through SSH and can inspect
systemd, SysV or process-based services, `/proc`, filesystems and bounded log
tails. A missing optional diagnostic command is reported as a skipped control;
it is not treated as proof that the device is broken.

HTTP, TCP and browser checks run from the Ubuntu computer. They therefore
validate the device as a client would see it and do not require test software
on the device. HTTP checks support expected statuses, redirects, content type,
content fragments, response headers (the endpoint `headers` map), JSON fields
and environment-backed Bearer/basic authentication. Request headers shared by
the configured endpoints belong under `http.headers` or `http.headers_from_env`.
The MPD/MPC playback check runs `mpc` over the existing SSH connection and
therefore validates the complete playback path on the Raspberry Pi 4.
Transmission is checked separately through its RPC endpoint and web interface.
The web-interface check uses Basic Auth from `TRANSMISSION_RPC_USERNAME` and
`TRANSMISSION_RPC_PASSWORD`, requires HTTP 200, Transmission content and rejects
the broken-page response containing `Couldn't find Transmission's web
interface files!`.

## Troubleshooting

- **No configuration found:** copy `config/test.example.yaml` to
  `config/test.yaml`, or pass `--config`.
- **SSH unavailable:** verify the route, address, port and that the device is
  booted. The connectivity test distinguishes reachability, authentication
  and host-key failures.
- **Host key rejected:** verify the fingerprint and update the Ubuntu user's
  `known_hosts`; never switch to an auto-accept policy.
- **A system check is skipped:** inspect whether the configured command exists
  in the minimal Yocto image, then adjust the configuration if that control is
  applicable.
- **HTTP failure:** check the endpoint from the Ubuntu computer with the same
  scheme, port, redirect policy and TLS trust expected by the test.
- **Browser launch failure:** install the optional requirements and Chromium
  dependencies in the Ubuntu test environment; no browser is required for
  the basic test suite.

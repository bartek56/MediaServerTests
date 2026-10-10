# TODO: Raspberry Pi 4 acceptance framework

## Framework work

- [x] YAML configuration with environment-variable overrides.
- [x] Reject unknown SSH host keys using `known_hosts`.
- [x] SSH command helper with result, timeout and safe argument quoting.
- [x] Connectivity, system, service, HTTP/HTTPS and optional browser checks.
- [x] Host-only unit tests using mocks.
- [x] HTML reporting support and ignored local artifacts.
- [x] Documentation for Ubuntu test hosts and Raspberry Pi 4 targets.
- [x] Initial `mediaserver-image-base` profile without Tvheadend and Nextcloud.
- [x] Base smoke checks for RPi4 identity, MPD handshake and Transmission RPC.

## Device-specific work before release acceptance

- [ ] Copy `config/test.example.yaml` to `config/test.yaml` and set the real
  target address, SSH user, key and host key.
- [ ] Confirm the SSH host fingerprint out of band before adding it to
  `~/.ssh/known_hosts`.
- [ ] Add only services, ports and endpoints present in the selected image.
- [ ] Set storage and memory thresholds for the test hardware.
- [ ] Decide whether Playwright checks are required and enable them explicitly.
- [ ] Review every failed or skipped check and record hardware limitations.

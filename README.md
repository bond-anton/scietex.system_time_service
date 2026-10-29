# scietex.system_time_service

Scietex microservice daemon that owns the host clock: timezone, NTP
configuration, manual clock setting, and RTC monitoring including battery state
where the hardware exposes it.

It runs as root on the host and is driven over Valkey by the API, which stays
unprivileged.

## Quick start

```bash
uv venv
uv pip install -e ".[test]"
.venv/bin/python -m pytest tests/ -q
```

Dry run against the current host without changing anything:

```bash
.venv/bin/start-system-time-service --dry-run --conf-dir /tmp/st-conf
```

## Documentation

- [`docs/deployment.md`](docs/deployment.md) — install, configuration, platform
  support, systemd unit, container (Podman), dry run.
- [`docs/wire-contract.md`](docs/wire-contract.md) — Valkey keys, telemetry
  structs, tasks, error codes.

## Container

A `Containerfile` is included. The image installs the checkout (the package is
not on PyPI), runs as root with no `USER` directive, and expects the host's
systemd, D-Bus, chrony socket, `/etc/os-release` and RTC to be mounted in:

```bash
podman build --build-arg VERSION=0.1.0 -t scietex-system-time-service:dev .
```

It is a **privileged host agent**, not a sandbox: `sudo podman`, `--pid=host`,
and the mount list in [Deployment](docs/deployment.md#container-podman) are all
required. Rootless podman cannot open `/dev/rtc0` or `/run/systemd/private`.

## Layout

```
src/scietex/system_time_service/
  clock/
    platform.py      OS detection and per-platform NTP drop-in layout
    capabilities.py  what this host and process can actually do
    runner.py        command execution seam (real / dry-run / recording)
    status.py        read-only parsers for timedatectl, chronyc, sysfs RTC
    commands.py      mutating operations with capability gating
    battery/         pluggable RTC battery providers
  config.py          settings struct and YAML bootstrap
  runtime.py         state shared by the worker and its handlers
  telemetry.py       telemetry payload
  task_handlers/     control-lane task handlers
  system_time_worker.py
  run_worker.py
```

# Deployment

The service must run as **root** on the host: setting the clock, writing NTP
drop-ins, and copying the system clock into the RTC all require privileges the
API container does not have. The API stays unprivileged and talks to this
worker over Valkey.

## Requirements

- Python >= 3.10
- `systemd` with `timedatectl`
- An NTP daemon: `chronyd` (Fedora/RHEL/Debian/Ubuntu) or `systemd-timesyncd`
- `util-linux` for `hwclock` (only needed to write the RTC)
- A reachable Valkey instance

## Install

```bash
uv venv
uv pip install scietex.system_time_service
```

Or from a checkout:

```bash
uv venv
uv pip install -e .
```

## Configuration

The service reads `<conf_dir>/system_time/system_time.yml` on first start and
writes a default file if it is absent. `--conf-dir` defaults to
`$SCIETEX_CONFIG_DIR`, then `$XDG_CONFIG_HOME/scietex`, then `/etc/scietex`.

```yaml
timezone: Europe/Moscow
clock_source: auto          # auto = NTP, manual = operator sets the clock
ntp_servers:
  - 0.pool.ntp.org
  - 1.pool.ntp.org
rtc_in_local_tz: false

# Host-local tuning, never delivered remotely.
status_interval: 5.0
rtc_device: /dev/rtc0
rtc_sysfs: /sys/class/rtc/rtc0
battery_provider: auto      # auto | none | sysfs | command
battery_command: null
battery_command_timeout: 3.0
```

Only `timezone`, `clock_source`, `ntp_servers`, and `rtc_in_local_tz` are
patchable through the API. The rest are host-local and must be edited in the
file.

### Battery provider

`auto` picks the first provider that can answer for this host:

1. On Raspberry Pi, the configured `battery_command` (the RTC cell is not a
   power supply there).
2. `sysfs` — a `type=battery` entry under `/sys/class/power_supply`.
3. The configured `battery_command`.
4. Otherwise unavailable.

`battery_command` must print a JSON object with any of `charge_percent`,
`voltage_volts`, `status`. This is the escape hatch for boards whose battery is
only reachable through a vendor tool; no per-board parsing lives in this
package.

## Platform support

The service reads `/etc/os-release` and `/proc/device-tree/model` and never
guesses. An unrecognised host is reported as unsupported rather than driven
with commands that may be wrong for it.

| Platform | NTP drop-in directory |
|---|---|
| Fedora / RHEL / Rocky / Alma | `/etc/chrony.d` |
| Debian | `/etc/chrony/conf.d` |
| Ubuntu >= 24.04 | `/etc/chrony/conf.d` |
| Ubuntu < 24.04 | not supported (reported) |
| Raspberry Pi OS | `/etc/chrony/conf.d` |
| anything else | not supported (reported) |

The drop-in is `scietex-ntp.conf` and carries a managed-by marker. The
distribution's own configuration is never edited, so deleting the drop-in
restores the distro defaults.

## systemd unit

```ini
[Unit]
Description=Scietex system time service
After=network-online.target chronyd.service
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/opt/scietex/system_time_service/.venv/bin/start-system-time-service \
    --conf-dir /etc/scietex --logging-level INFO
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now scietex-system-time
```

## Dry run

`--dry-run` executes read-only commands for real and records mutating ones
without running them. Use it to verify the service against a host without
changing anything, and to run it as a non-root user during development:

```bash
start-system-time-service --dry-run --conf-dir /tmp/st-conf --logging-level DEBUG
```

In dry-run mode every mutating operation returns `PERMISSION_DENIED` when the
process is unprivileged, and `DRY_RUN` when it is privileged but the runner is
recording. Telemetry is published normally, so the UI can be exercised against
a dry-run worker.

## Valkey keys

| Key | Content |
|---|---|
| `scietex:SystemTime:config` | config envelope, section `services["system_time"]` |
| `scietex:SystemTime:{instance}:status` | heartbeat (framework) |
| `scietex:SystemTime:{instance}:telemetry` | msgpack telemetry, 2 s interval, 6 s TTL |
| `scietex:SystemTime:control:{instance}` | control stream |
| `scietex:SystemTime:tasks` | task queue |

## Tasks

| Task | Lane | Purpose |
|---|---|---|
| `system_time:status` | control | current telemetry snapshot |
| `system_time:read_rtc` | control | RTC reading alone |
| `system_time:apply` | control | re-apply the current settings |
| `system_time:set_clock` | control | set the clock to an absolute instant |

`system_time:set_clock` refuses when `clock_source` is `auto`: setting the
clock while an NTP daemon is slewing it produces a fight the operator cannot
see.

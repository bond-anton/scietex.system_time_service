# Deployment

The service must run as **root** on the host: setting the clock, writing NTP
drop-ins, and copying the system clock into the RTC all require privileges the
API container does not have. The API stays unprivileged and talks to this
worker over Valkey.

A container image exists for hosts deployed that way; it is the same root
daemon with the host's systemd, D-Bus, chrony and RTC bind-mounted in. See
[Container (Podman)](#container-podman) — it is a privileged host agent, not a
sandbox.

## Requirements

- Python >= 3.10
- `systemd` with `timedatectl`
- An NTP daemon: `chronyd` (Fedora/RHEL/Debian/Ubuntu) or `systemd-timesyncd`
- `util-linux` for `hwclock` (only needed to write the RTC; on Debian trixie the
  binary is in `util-linux-extra`)
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
rtc_sync_interval_seconds: null  # 60..86400, null disables the periodic RTC write

# Host-local tuning, never delivered remotely.
status_interval: 5.0
rtc_device: /dev/rtc0
rtc_sysfs: /sys/class/rtc/rtc0
battery_provider: auto      # auto | none | sysfs | command
battery_command: null
battery_command_timeout: 3.0
```

Only `timezone`, `clock_source`, `ntp_servers`, `rtc_in_local_tz`, and
`rtc_sync_interval_seconds` are patchable through the API. The rest are
host-local and must be edited in the file.

`rtc_device` is the node the service opens to write the RTC. The default
`/dev/rtc0` matches a bare-metal install. A container that maps the host node
onto `/dev/rtc` must set `rtc_device: /dev/rtc` to match — see
[The RTC device path](#the-rtc-device-path).

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

## Container (Podman)

The image is a **privileged host agent, not a sandbox**. It runs as root, reaches
the host's systemd through mounted sockets, writes the host clock and the RTC,
and rewrites the host's NTP drop-ins. Run **one instance per host**, and treat
the image and everything it reads from its environment exactly as you would treat
a root daemon installed on the host.

### Why root

`is_privileged()` is `os.geteuid() == 0`, and the capability set reports
`timezone_settable`, `clock_settable` and `ntp_controllable` only when
`timedatectl` is present and the effective uid is 0. An unprivileged container
starts, publishes telemetry, and then refuses every mutating task.

The image carries no `USER` directive, and podman must run **rootful**. Rootless
podman maps container-root to the calling user, which cannot open `/dev/rtc0`
(`root:clock`, mode 0640) or `/run/systemd/private` (root-only socket). Every
command below uses `sudo podman`.

### Why no systemd inside

The `systemd` package is installed for its client tools only: `timedatectl` and
`systemctl`. PID 1 in the container is the worker; no init system runs. Every
systemd call is a request to the **host's** manager over the mounted
`/run/systemd/private` socket, so the units being driven are the host's
`chronyd`, `systemd-timesyncd` and `systemd-timedated`.

### Why `--pid=host`

`systemctl` reaches host PID 1 through `/run/systemd/private`, and the peer
credentials on that socket cannot be resolved across a PID namespace boundary —
without `--pid=host` every call fails with `Operation not permitted`. It also
makes `systemctl restart chronyd` act on the host's units rather than on a
namespace it cannot see.

### Mounts

| Host path | Mode | Why |
| --- | --- | --- |
| `/run/systemd/system` | `:ro` | `sd_booted()` probe; without it the systemd clients refuse to act |
| `/run/dbus` | `:ro` | the system bus `timedatectl`/`systemctl` fall back to |
| `/run/systemd/private` | `:ro` | `systemctl`'s direct channel to PID 1; it does not fall back to the bus |
| `/run/chrony` | `:ro` | `chronyc` queries `chronyd` over `/run/chrony/chronyd.sock` |
| `/etc/os-release` | `:ro` | `detect_platform` must classify the host, not the Debian base image |
| `/etc/chrony.d` (Fedora/RHEL) or `/etc/chrony/conf.d` (Debian/Ubuntu/RPi) | `:rw,z` | where the `scietex-ntp.conf` drop-in is written |
| `/etc/systemd/timesyncd.conf.d` | `:rw,z` | the drop-in directory on hosts that run timesyncd |
| `/etc/adjtime` | `:rw,z` | optional (see below); keeps hwclock drift correction consistent with the host |
| `/etc/scietex` → `/config` | `:rw,z` | `system_time.yml` and `valkey.yml` |

`/sys` needs no explicit mount: podman already mounts it read-only, which is
enough for `/sys/class/rtc/rtc0` (the RTC snapshot) and
`/sys/class/power_supply` (the battery provider).

`/etc/adjtime` is optional. Mounting it lets `hwclock` accumulate drift
correction in the host's file instead of a throwaway one inside the container,
so the drift estimate stays consistent with the host's own `hwclock` runs. The
file must already exist: podman creates a missing bind source as a *directory*,
and `hwclock` then fails on it. Drop the mount if the host has never written one.

### The RTC device path

`--device /dev/rtc0:/dev/rtc` maps the host node onto **`/dev/rtc`**, not
`/dev/rtc0`. The service's default `rtc_device` is `/dev/rtc0`, so a container
deployment **must** override it in `system_time.yml`:

```yaml
rtc_device: /dev/rtc
```

Without that line the service probes a path that does not exist inside the
container. RTC *reads* still work, because they go through
`/sys/class/rtc/rtc0`, but `rtc_writable` reports `false` and the periodic sync
is unavailable. `hwclock --systohc` is called with no `--rtc` and falls back to
its built-in default, which is `/dev/rtc` — so the mapping and the config must
agree.

### What `rtc_writable` actually checks

`rtc_writable` is true only when all three hold:

1. `/sys/class/rtc/rtc0/since_epoch` exists (`rtc_present`),
2. the process can open `rtc_device` read-write (`os.access`), and
3. the effective capability set contains `CAP_SYS_TIME`.

The third check exists because the kernel gates the `RTC_SET_TIME` ioctl on
`CAP_SYS_TIME` in the **initial** user namespace. A rootless container can open
the device node and still fail the write with `EPERM`, and `--cap-add SYS_TIME`
does not help: the capability lands in `CapEff` but the ioctl is still refused,
because the container's user namespace is not the initial one. Checking file
permissions alone would report a writable RTC that cannot be written.

### Run

```bash
# Run as root. Rootless podman cannot open /dev/rtc0 or /run/systemd/private.
sudo mkdir -p /etc/chrony.d /etc/scietex

sudo podman run -d --name scietex-system-time \
  --restart=unless-stopped \
  --network host \
  --pid=host \
  --cap-add SYS_TIME \
  --device /dev/rtc0:/dev/rtc \
  -v /run/systemd/system:/run/systemd/system:ro \
  -v /run/dbus:/run/dbus:ro \
  -v /run/systemd/private:/run/systemd/private:ro \
  -v /run/chrony:/run/chrony:ro \
  -v /etc/os-release:/etc/os-release:ro \
  -v /etc/chrony.d:/etc/chrony.d:z \
  -v /etc/systemd/timesyncd.conf.d:/etc/systemd/timesyncd.conf.d:z \
  -v /etc/adjtime:/etc/adjtime:z \
  -v /etc/scietex:/config:z \
  registry.buro-nts.ru/scietex-system-time-service:0.1.0
```

- **`--device /dev/rtc0:/dev/rtc` maps onto `/dev/rtc`, not `/dev/rtc0`.**
  Set `rtc_device: /dev/rtc` in `system_time.yml` to match (see above).
  Mapping only `/dev/rtc0` leaves RTC *writes* failing with
  `Cannot access the Hardware Clock via any known method` while *reads* still
  work through `/sys` — a partially working service that looks healthy until
  something depends on the RTC.
- On Debian, Ubuntu and Raspberry Pi OS hosts use
  `-v /etc/chrony/conf.d:/etc/chrony/conf.d:z` instead of the `/etc/chrony.d`
  line, and create that directory instead.
- `--network host` because Valkey usually runs on the same host and
  `/config/valkey.yml` defaults to `localhost:6379`. Drop it and set `host:` in
  `valkey.yml` if Valkey is remote.
- `--cap-add SYS_TIME` is defensive. The current paths work without it — the
  host's systemd performs the operations, and the RTC write is gated by the
  device node — but granting it means a future in-container clock set cannot
  fail with EPERM.
- Do **not** add `--privileged`. The explicit list above is sufficient and
  auditable; a blanket privileged container grants far more than this service
  needs.
- SELinux hosts need `:z` on the writable mounts. Do not use `:Z` on
  `/etc/scietex` if that tree is shared with other containers — a private MCS
  category relabels it, and whichever container started last locks the others
  out until the next restart flips the label back.
- **Raspberry Pi caveat:** `/proc/device-tree` is absent and `/sys/firmware` is
  masked inside the container, so a Pi is classified `debian` with
  `is_raspberry_pi=False`. The chrony paths are identical, but the Pi-specific
  battery branch is skipped — set `battery_provider: command` explicitly in
  `system_time.yml` if the RTC cell is only reachable through a vendor tool.

### Rootless podman (user quadlet)

A rootless deployment is possible but **degraded**. It is worth running when you
want the host's timezone, clock and RTC state reported to the UI and do not need
NTP control or RTC writes.

| Capability | Rootful | Rootless |
| --- | --- | --- |
| `timedatectl` reads | yes | yes, with `--pid=host` |
| timezone set | yes | yes |
| clock set | yes | yes |
| RTC read | yes | yes (via `/sys`) |
| RTC write | yes | **no** — `RTC_SET_TIME` needs `CAP_SYS_TIME` in the initial user namespace |
| NTP control (`chronyc`) | yes | **no** — `/run/chrony` is `chrony:chrony 0750` |
| NTP config (drop-ins) | yes | **no** — `/etc/chrony.d` is root-owned |
| `ntp_daemon` detection | yes | **no** — `systemctl` needs `/run/systemd/private`, `srwx------` root-only |

`--pid=host` is mandatory. Without it `timedatectl` fails with
`Operation not permitted` even with `/run/dbus` mounted, because it prefers
`/run/systemd/private` and does not fall back to the bus.

#### Making the RTC device readable

The device node is `root:clock` mode 0640. A rootless container's gid map is
built from `/etc/subgid`, and the host's `clock` gid is normally outside that
range, so the node appears as `nobody:nogroup` and `open()` fails with `EACCES`.
Two root changes fix the read path:

```bash
sudo usermod -aG clock <user>
echo '<user>:103:1' | sudo tee -a /etc/subgid
```

`103` is the host's `clock` gid — check with `getent group clock`. The
`usermod` alone does nothing for the container; the `/etc/subgid` line is what
puts the gid into the namespace map. After editing `/etc/subgid`, run
`podman system migrate` so the running podman picks up the new mapping (it
caches the map at startup). The group change also needs a fresh login session
before `id -nG` shows it.

With the mapping in place the container's gid map gains `1 -> 103`, and the
device resolves to `nobody:daemon` (Debian's name for gid 1). The container
process needs that gid in its supplementary set, so add `GroupAdd=1` to the
quadlet — the container-side gid, not the host's 103.

This buys RTC **reads** through the device node. Reads already worked via
`/sys`, and writes remain impossible, so the practical gain is small. Skip it
unless something needs the device node itself.

#### Quadlet

```ini
[Unit]
Description=System Time Service Container
Requires=valkey.service
After=valkey.service

[Container]
ContainerName=system-time
Image=registry.buro-nts.ru/scietex-system-time-service:0.1.0
# timedatectl reaches the host's systemd only through /run/systemd/private,
# whose peer credentials cannot be resolved across a PID namespace. Without
# --pid=host every call fails with EPERM even with the bus mounted.
PodmanArgs=--pid=host
# The container-side gid for the host's clock group, once /etc/subgid maps it.
GroupAdd=1
Volume=%h/.config/scietex-system-time:/config:z
Volume=/run/systemd/system:/run/systemd/system:ro
Volume=/run/dbus:/run/dbus:ro
Volume=/etc/os-release:/etc/os-release:ro
AutoUpdate=registry

[Install]
WantedBy=default.target
```

The config directory needs `system_time.yml` with `rtc_device: /dev/rtc` and a
`valkey.yml` pointing at the host's Valkey. A rootless container reaches the
host through `host.containers.internal`, not `localhost`:

```yaml
base_config:
  nodes:
    - host: host.containers.internal
      port: 6379
```

Do not mount `/run/systemd/private` or `/run/chrony` in a rootless quadlet.
Both are root-only, and mounting them read-only does not change that — the
service reports `ntp_daemon: none` and `ntp_configurable: false` either way.

### Build

```bash
./build_image.sh            # multi-arch manifest, pushes the version tag
./build_image.sh --latest   # also pushes :latest
```

The script reads the version from `src/scietex/system_time_service/version.py`,
builds `linux/amd64,linux/arm64` into a temporary manifest, pushes
`registry.buro-nts.ru/scietex-system-time-service:<version>`, and removes the
manifest again.

The image **builds from the checkout** — the package is not published on PyPI, so
there is no released artifact to pin. To stop a tag from lying about its
contents, the Containerfile compares the `VERSION` build arg with the installed
distribution metadata and fails the build on a mismatch.

A local single-arch build:

```bash
podman build --build-arg VERSION=0.1.0 -t scietex-system-time-service:dev .
```

### Verify before enabling

`--dry-run` executes read-only commands for real and records mutating ones
without running them, so it exercises every probe — `timedatectl show`,
`chronyc`, the RTC sysfs read, platform detection — while touching neither the
clock, the RTC, nor the NTP drop-ins. It may still create its config file on
first run. Run it before the service itself, or stop the service first; two
workers sharing the same Valkey keys fight over the status and telemetry keys.

```bash
run_args=(
  --network host --pid=host --cap-add SYS_TIME --device /dev/rtc0:/dev/rtc
  -v /run/systemd/system:/run/systemd/system:ro
  -v /run/dbus:/run/dbus:ro
  -v /run/systemd/private:/run/systemd/private:ro
  -v /run/chrony:/run/chrony:ro
  -v /etc/os-release:/etc/os-release:ro
  -v /etc/chrony.d:/etc/chrony.d:z
  -v /etc/systemd/timesyncd.conf.d:/etc/systemd/timesyncd.conf.d:z
  -v /etc/adjtime:/etc/adjtime:z
  -v /etc/scietex:/config:z
)

sudo podman run --rm -it "${run_args[@]}" \
  registry.buro-nts.ru/scietex-system-time-service:0.1.0 \
  start-system-time-service --dry-run --logging-level DEBUG
```

Then confirm the running container sees the host, not the image:

```bash
sudo podman exec scietex-system-time timedatectl show
sudo podman exec scietex-system-time systemctl is-active chronyd
sudo podman exec scietex-system-time chronyc -c tracking
sudo podman exec scietex-system-time head -2 /etc/os-release   # must be the host
```

`head -2 /etc/os-release` is the load-bearing check: if it prints the container
distribution, the mount is missing and the service will write chrony drop-ins to
a directory nothing on the host reads.

### Optional Quadlet

For a persistent deployment, run the container as a **rootful** systemd service
via a Quadlet — `/etc/containers/systemd/scietex-system-time.container`:

```ini
[Unit]
Description=Scietex system time service container
After=chronyd.service network-online.target
Wants=network-online.target

[Container]
ContainerName=scietex-system-time
Image=registry.buro-nts.ru/scietex-system-time-service:latest
Network=host
# No UserNS= mapping: the worker must be real root, not the calling user.
PodmanArgs=--pid=host
AddCapability=SYS_TIME
AddDevice=/dev/rtc0:/dev/rtc
Volume=/run/systemd/system:/run/systemd/system:ro
Volume=/run/dbus:/run/dbus:ro
Volume=/run/systemd/private:/run/systemd/private:ro
Volume=/run/chrony:/run/chrony:ro
Volume=/etc/os-release:/etc/os-release:ro
Volume=/etc/chrony.d:/etc/chrony.d:z
Volume=/etc/systemd/timesyncd.conf.d:/etc/systemd/timesyncd.conf.d:z
Volume=/etc/adjtime:/etc/adjtime:z
Volume=/etc/scietex:/config:z
AutoUpdate=registry

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /etc/chrony.d /etc/scietex /etc/systemd/timesyncd.conf.d
sudo systemctl daemon-reload
sudo systemctl start scietex-system-time.service
```

Set `rtc_device: /dev/rtc` in `/etc/scietex/system_time/system_time.yml` to
match `AddDevice=/dev/rtc0:/dev/rtc`; the default `/dev/rtc0` does not exist
inside the container.

`PodmanArgs=--pid=host` is required because Quadlet has no dedicated key for the
PID namespace; `After=chronyd.service` only orders the container against the
daemon it configures, it does not depend on it — the worker tolerates chrony
restarts and re-queries it.

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

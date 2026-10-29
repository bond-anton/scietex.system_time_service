# Stage 1: build the virtual environment from the checkout.
# Set the base image using Python 3.14 and Debian Trixie (current stable)
FROM python:3.14-slim-trixie AS builder

# Version the image claims to be. build_image.sh passes the version read from
# src/scietex/system_time_service/version.py so the OCI label and the packaged
# code cannot drift apart.
ARG VERSION="dev"

WORKDIR /build

# The package is not published on PyPI, so unlike the sibling modbus image this
# one installs the source from the build context instead of a released wheel.
COPY pyproject.toml README.md ./
COPY src ./src

# Only this pure-Python project is compiled here, and every runtime dependency
# ships a cp314 manylinux wheel, so no compiler or Python headers are needed.
# Then slim the venv: pip/setuptools/wheel are build-time only, bytecode caches
# are regenerated on first import, and valkey-glide ships one extension per
# supported interpreter while only the running interpreter's is ever loaded.
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -U pip && \
    /opt/venv/bin/pip install --no-cache-dir . && \
    /opt/venv/bin/pip uninstall -y pip setuptools wheel && \
    find /opt/venv -name '*.pyc' -delete && \
    find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + && \
    find /opt/venv/lib/python3.14/site-packages/glide_shared -name '*.so' \
        ! -name '*cpython-314*' ! -name 'libglide_ffi.so' -delete

# The image ships the checkout rather than a published artifact, so a tag could
# otherwise disagree with the code inside it. Fail the build instead of
# publishing a mislabelled image.
RUN if [ "${VERSION}" != "dev" ]; then \
        installed=$(/opt/venv/bin/python -c \
            'import importlib.metadata as m; print(m.version("scietex.system_time_service"))') && \
        if [ "${installed}" != "${VERSION}" ]; then \
            echo "Error: VERSION build arg is ${VERSION} but src/scietex/system_time_service/version.py reports ${installed}" >&2; \
            echo "Bump the version file or pass the matching --build-arg VERSION." >&2; \
            exit 1; \
        fi; \
    fi

# Stage 2: Runtime image
FROM python:3.14-slim-trixie

ARG VERSION="dev"

LABEL org.opencontainers.image.title="scietex-system-time-service" \
      org.opencontainers.image.description="Scietex worker for host clock, timezone, NTP and RTC management" \
      org.opencontainers.image.version="${VERSION}"

# systemd: client tools only (timedatectl, systemctl). The image never starts an
# init system — PID 1 is the worker — and every call is a request to the host's
# manager over the mounted /run/systemd/private socket.
# chrony: chronyc for the tracking query, and the daemon whose drop-in directory
# this service writes into on the host.
# util-linux-extra: on trixie hwclock lives here, not in util-linux. Installing
# plain util-linux leaves `hwclock` missing.
# tzdata is deliberately absent: the base image already ships
# /usr/share/zoneinfo, and timezone changes are executed by the host's systemd.
RUN apt-get update && apt-get install -y --no-install-recommends \
    systemd \
    chrony \
    util-linux-extra \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Configuration lives in a mounted volume so it survives container replacement.
# SCIETEX_CONFIG_DIR is honored by the framework's prepare_conf_dir, which
# silently skips a set-but-nonexistent directory — hence the mkdir. The service
# namespaces its files under <config-dir>/system_time/.
ENV SCIETEX_CONFIG_DIR=/config \
    SCIETEX_SERVICE_NAME=SystemTime \
    SCIETEX_LOGGING_LEVEL=INFO
RUN mkdir -p /config
VOLUME ["/config"]

# No USER directive: is_privileged() is os.geteuid() == 0, and every mutating
# operation (timedatectl set-time, hwclock --systohc, drop-in writes) is gated on
# it. This image is a privileged host agent, not a sandboxed service like the
# sibling modbus one with its appuser.
CMD ["start-system-time-service"]

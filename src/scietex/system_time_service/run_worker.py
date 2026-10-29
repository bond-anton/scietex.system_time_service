"""Console entry point for the system time service."""

import argparse
import asyncio
import os

from scietex.service import ValkeyWorkerConfig

from .config import SYSTEM_TIME_CONFIG_SUBDIR
from .system_time_worker import SystemTimeWorker
from .version import __version__

ENV_SERVICE_NAME: str = "SCIETEX_SERVICE_NAME"
ENV_LOGGING_LEVEL: str = "SCIETEX_LOGGING_LEVEL"
DEFAULT_SERVICE_NAME: str = "SystemTime"
DEFAULT_LOGGING_LEVEL: str = "INFO"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scietex system time service")
    parser.add_argument("--conf-dir", default=None, help="Configuration directory")
    parser.add_argument(
        "--service-name",
        default=os.environ.get(ENV_SERVICE_NAME, DEFAULT_SERVICE_NAME),
        help=f"Service name (default: ${ENV_SERVICE_NAME} or {DEFAULT_SERVICE_NAME})",
    )
    parser.add_argument(
        "--logging-level",
        default=os.environ.get(ENV_LOGGING_LEVEL, DEFAULT_LOGGING_LEVEL),
        help=f"Logging level (default: ${ENV_LOGGING_LEVEL} or {DEFAULT_LOGGING_LEVEL})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute read-only commands for real and record mutating ones without running them",
    )
    return parser


def _build_config(args: argparse.Namespace) -> ValkeyWorkerConfig:
    return ValkeyWorkerConfig(
        service_name=args.service_name,
        version=__version__,
        conf_dir=args.conf_dir,
        logging_level=args.logging_level,
        remote_config_enabled=True,
        valkey_config=None,
        config_file=f"{SYSTEM_TIME_CONFIG_SUBDIR}/config.yml",
    )


def main() -> None:
    args = _build_parser().parse_args()
    config = _build_config(args)

    async def run() -> None:
        worker = SystemTimeWorker(config, dry_run=args.dry_run)
        await worker.start()
        await worker.events["exit"].wait()

    asyncio.run(run())


if __name__ == "__main__":
    main()

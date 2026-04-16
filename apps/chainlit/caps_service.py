from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path

from caps import CapsFTPServerRuntime, ensure_caps_storage_root_ready
from settings import SETTINGS

LOGGER = logging.getLogger("ncoai.caps-ftp")


def fixed_game_id() -> int:
    game_id = SETTINGS.team_token_auth.fixed_game_id
    if game_id <= 0:
        raise ValueError("GZCTF_FIXED_GAME_ID must be a positive integer")
    return game_id


def caps_storage_root() -> Path:
    return Path(SETTINGS.caps.storage_root).expanduser()


def caps_game_root() -> Path:
    return caps_storage_root() / f"game-{fixed_game_id()}"


def build_caps_ftp_runtime() -> CapsFTPServerRuntime:
    storage_root = caps_game_root()
    return CapsFTPServerRuntime(
        host=SETTINGS.caps.ftp_host,
        port=SETTINGS.caps.ftp_port,
        username=SETTINGS.caps.ftp_username,
        password=SETTINGS.caps.ftp_password,
        storage_root=storage_root,
        passive_ports=SETTINGS.caps.passive_ports,
        public_host=SETTINGS.caps.ftp_public_host,
        tls_enabled=SETTINGS.caps.ftp_tls_enabled,
        tls_certfile=SETTINGS.caps.ftp_tls_certfile,
        tls_keyfile=SETTINGS.caps.ftp_tls_keyfile,
        tls_control_required=SETTINGS.caps.ftp_tls_control_required,
        tls_data_required=SETTINGS.caps.ftp_tls_data_required,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )

    if not SETTINGS.caps.enabled:
        LOGGER.info("CAPS FTP disabled; exiting without starting the service")
        return 0

    storage_root = caps_game_root()
    ensure_caps_storage_root_ready(storage_root)
    runtime = build_caps_ftp_runtime()
    runtime.start()
    LOGGER.info(
        "CAPS FTP service started on %s:%s storage_root=%s passive_ports=%s-%s",
        SETTINGS.caps.ftp_host,
        runtime.port or SETTINGS.caps.ftp_port,
        storage_root,
        SETTINGS.caps.ftp_passive_port_start,
        SETTINGS.caps.ftp_passive_port_end,
    )

    stop_event = threading.Event()

    def request_stop(signum: int, _frame) -> None:
        LOGGER.info("Received signal %s, stopping CAPS FTP service", signum)
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    try:
        stop_event.wait()
    finally:
        runtime.stop()
        LOGGER.info("CAPS FTP service stopped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

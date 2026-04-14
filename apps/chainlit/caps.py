from __future__ import annotations

import ipaddress
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any


def ensure_caps_storage_root_ready(storage_root: Path) -> None:
    storage_root.mkdir(parents=True, exist_ok=True)
    if not storage_root.is_dir():
        raise RuntimeError(f"caps storage root is not a directory: {storage_root}")

    with tempfile.NamedTemporaryFile(
        dir=storage_root,
        prefix=".caps-write-check-",
        delete=True,
    ):
        pass


def normalize_ftp_public_host(public_host: str | None) -> str | None:
    normalized = str(public_host or "").strip()
    if not normalized:
        return None

    try:
        return str(ipaddress.IPv4Address(normalized))
    except ipaddress.AddressValueError:
        pass

    try:
        addresses = socket.getaddrinfo(
            normalized,
            None,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise RuntimeError(
            f"caps FTP public host could not be resolved to IPv4: {normalized}"
        ) from exc

    for family, _socktype, _proto, _canonname, sockaddr in addresses:
        if family == socket.AF_INET:
            return str(ipaddress.IPv4Address(sockaddr[0]))

    raise RuntimeError(
        f"caps FTP public host must resolve to an IPv4 address: {normalized}"
    )


class CapsFTPServerRuntime:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        storage_root: Path,
        passive_ports: tuple[int, ...] | None = None,
        public_host: str | None = None,
        tls_enabled: bool = False,
        tls_certfile: str | None = None,
        tls_keyfile: str | None = None,
        tls_control_required: bool = True,
        tls_data_required: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._storage_root = storage_root.expanduser().resolve()
        self._passive_ports = tuple(passive_ports or ())
        self._public_host = str(public_host or "").strip() or None
        self._tls_enabled = bool(tls_enabled)
        self._tls_certfile = (
            str(tls_certfile).strip() if tls_certfile is not None else None
        ) or None
        self._tls_keyfile = (
            str(tls_keyfile).strip() if tls_keyfile is not None else None
        ) or None
        self._tls_control_required = bool(tls_control_required)
        self._tls_data_required = bool(tls_data_required)
        self._server: Any | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        if self._server is None:
            return None
        return int(self._server.socket.getsockname()[1])

    def start(self) -> None:
        if self._server is not None:
            return
        if not str(self._host or "").strip():
            raise RuntimeError("caps FTP host is required")
        if self._port < 0 or self._port > 65535:
            raise RuntimeError("caps FTP port must be between 0 and 65535")
        if not self._username or not self._password:
            raise RuntimeError("caps FTP credentials are required")
        if not self._passive_ports:
            raise RuntimeError("caps FTP passive port range is required")
        if self._tls_enabled and not self._tls_certfile:
            raise RuntimeError("caps FTPS certfile is required when TLS is enabled")
        if self._tls_enabled and not self._tls_keyfile:
            raise RuntimeError("caps FTPS keyfile is required when TLS is enabled")
        if self._tls_enabled and self._tls_certfile and not Path(self._tls_certfile).is_file():
            raise RuntimeError(
                f"caps FTPS certfile does not exist: {self._tls_certfile}"
            )
        if self._tls_enabled and self._tls_keyfile and not Path(self._tls_keyfile).is_file():
            raise RuntimeError(
                f"caps FTPS keyfile does not exist: {self._tls_keyfile}"
            )

        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.servers import FTPServer
            if self._tls_enabled:
                from pyftpdlib.handlers.ftps.control import TLS_FTPHandler
            else:
                from pyftpdlib.handlers import FTPHandler
        except ImportError as exc:  # pragma: no cover - runtime dependency path
            if self._tls_enabled:
                raise RuntimeError(
                    "pyftpdlib FTPS support requires pyOpenSSL to be installed"
                ) from exc
            raise RuntimeError("pyftpdlib is required for caps FTP support") from exc

        masquerade_address = normalize_ftp_public_host(self._public_host)

        authorizer = DummyAuthorizer()
        authorizer.add_user(
            self._username,
            self._password,
            str(self._storage_root),
            perm="elwa",
        )

        if self._tls_enabled:
            handler_base = TLS_FTPHandler
        else:
            handler_base = FTPHandler

        class InternalCapsFTPHandler(handler_base):
            pass

        InternalCapsFTPHandler.authorizer = authorizer
        InternalCapsFTPHandler.banner = "NCOAI caps FTP ready."
        InternalCapsFTPHandler.passive_ports = list(self._passive_ports)
        InternalCapsFTPHandler.masquerade_address = masquerade_address
        InternalCapsFTPHandler.permit_foreign_addresses = False
        if self._tls_enabled:
            InternalCapsFTPHandler.certfile = self._tls_certfile
            InternalCapsFTPHandler.keyfile = self._tls_keyfile
            InternalCapsFTPHandler.tls_control_required = self._tls_control_required
            InternalCapsFTPHandler.tls_data_required = self._tls_data_required

        server = FTPServer((self._host, self._port), InternalCapsFTPHandler)
        server.max_cons = 32
        server.max_cons_per_ip = 8

        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"timeout": 0.5, "blocking": True, "handle_exit": False},
            name="ncoai-caps-ftp",
            daemon=True,
        )
        thread.start()

        self._server = server
        self._thread = thread

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.close_all()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

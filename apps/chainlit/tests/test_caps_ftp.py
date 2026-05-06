from __future__ import annotations

import io
import ipaddress
import ssl
import sys
from datetime import UTC, datetime, timedelta
from dataclasses import replace
from ftplib import FTP, FTP_TLS, error_perm
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyftpdlib.filesystems import AbstractedFS

APP_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = APP_DIR.parents[1]

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app as chainlit_app  # noqa: E402
from caps import CapsFTPServerRuntime, normalize_ftp_public_host  # noqa: E402


@pytest.fixture
def caps_mode(monkeypatch, tmp_path):
    storage_root = tmp_path / "caps"
    caps_settings = replace(
        chainlit_app.SETTINGS.caps,
        enabled=True,
        storage_root=str(storage_root),
        ftp_host="127.0.0.1",
        ftp_port=0,
        ftp_username="nco_ftp",
        ftp_password="uploadcaps0731!",
        ftp_admin_username="admin",
        ftp_admin_password="nervergonnagiveyouup",
        ftp_passive_port_start=42100,
        ftp_passive_port_end=42109,
        ftp_public_host=None,
        ftp_tls_enabled=False,
        ftp_tls_certfile=None,
        ftp_tls_keyfile=None,
        ftp_tls_control_required=True,
        ftp_tls_data_required=True,
    )
    monkeypatch.setattr(
        chainlit_app,
        "SETTINGS",
        replace(
            chainlit_app.SETTINGS,
            team_token_auth=replace(
                chainlit_app.SETTINGS.team_token_auth,
                fixed_game_id=2026,
            ),
            caps=caps_settings,
            chainlit_root_path="/chat",
        ),
    )
    repository = SimpleNamespace(
        touch_managed_user_login=AsyncMock(),
    )
    monkeypatch.setattr(chainlit_app, "CONTROL_PLANE_REPOSITORY", repository)
    monkeypatch.setattr(chainlit_app, "CAPS_FTP_RUNTIME", None)
    return {
        "storage_root": storage_root,
        "game_root": storage_root / "game-2026",
        "repository": repository,
    }


def test_caps_routes_are_not_registered() -> None:
    assert not chainlit_app.chainlit_route_exists("/caps", "GET")
    assert not chainlit_app.chainlit_route_exists("/caps/api/upload", "POST")
    assert not chainlit_app.chainlit_route_exists("/chat/caps", "GET")


def test_chainlit_lifespan_callbacks_are_registered() -> None:
    assert chainlit_app.config.code.on_app_startup is chainlit_app.chainlit_app_startup
    assert chainlit_app.config.code.on_app_shutdown is chainlit_app.chainlit_app_shutdown


@pytest.mark.asyncio
async def test_startup_caps_runtime_prepares_storage_and_starts_ftp(
    caps_mode,
    monkeypatch,
) -> None:
    fake_runtime = SimpleNamespace(start=Mock(), stop=Mock())
    monkeypatch.setattr(chainlit_app, "build_caps_ftp_runtime", lambda: fake_runtime)

    await chainlit_app.startup_caps_runtime()

    assert caps_mode["game_root"].is_dir()
    fake_runtime.start.assert_called_once()
    assert chainlit_app.CAPS_FTP_RUNTIME is fake_runtime

    await chainlit_app.shutdown_caps_runtime()
    fake_runtime.stop.assert_called_once()
    assert chainlit_app.CAPS_FTP_RUNTIME is None


@pytest.mark.asyncio
async def test_startup_caps_runtime_surfaces_ftp_start_failures(
    caps_mode,
    monkeypatch,
) -> None:
    class BrokenRuntime:
        def start(self) -> None:
            raise RuntimeError("port already in use")

    monkeypatch.setattr(chainlit_app, "build_caps_ftp_runtime", lambda: BrokenRuntime())

    with pytest.raises(RuntimeError, match="port already in use"):
        await chainlit_app.startup_caps_runtime()


def test_build_caps_ftp_runtime_uses_fixed_credentials(caps_mode) -> None:
    runtime = chainlit_app.build_caps_ftp_runtime()

    assert isinstance(runtime, CapsFTPServerRuntime)
    assert runtime._username == "nco_ftp"
    assert runtime._password == "uploadcaps0731!"
    assert runtime._admin_username == "admin"
    assert runtime._admin_password == "nervergonnagiveyouup"
    assert runtime._storage_root == caps_mode["game_root"]
    assert runtime._tls_enabled is False


def test_caps_ftp_runtime_supports_fixed_account_upload_only_permissions(tmp_path) -> None:
    storage_root = tmp_path / "caps"
    storage_root.mkdir()

    runtime = CapsFTPServerRuntime(
        host="127.0.0.1",
        port=0,
        username="nco_ftp",
        password="uploadcaps0731!",
        admin_username="admin",
        admin_password="nervergonnagiveyouup",
        storage_root=storage_root,
        passive_ports=tuple(range(42110, 42120)),
    )

    runtime.start()
    ftp = FTP()
    try:
        assert runtime.port is not None
        ftp.connect("127.0.0.1", runtime.port, timeout=5)
        ftp.login("nco_ftp", "uploadcaps0731!")
        assert ftp.pwd() == "/"

        ftp.storbinary("STOR solve.mp4", io.BytesIO(b"abc"))
        ftp.storbinary("APPE solve.mp4", io.BytesIO(b"def"))
        assert ftp.nlst() == ["solve.mp4"]

        stored_path = storage_root / "solve.mp4"
        assert stored_path.read_bytes() == b"abcdef"

        with pytest.raises(error_perm):
            ftp.retrbinary("RETR solve.mp4", lambda _: None)
        with pytest.raises(error_perm):
            ftp.delete("solve.mp4")
        with pytest.raises(error_perm):
            ftp.rename("solve.mp4", "renamed.mp4")

        jailed_fs = AbstractedFS(str(storage_root), None)
        assert jailed_fs.validpath(str(storage_root.parent)) is False
    finally:
        try:
            ftp.close()
        finally:
            runtime.stop()


def test_caps_ftp_runtime_supports_admin_account_full_permissions(tmp_path) -> None:
    storage_root = tmp_path / "caps"
    storage_root.mkdir()

    runtime = CapsFTPServerRuntime(
        host="127.0.0.1",
        port=0,
        username="nco_ftp",
        password="uploadcaps0731!",
        admin_username="admin",
        admin_password="nervergonnagiveyouup",
        storage_root=storage_root,
        passive_ports=tuple(range(42140, 42150)),
    )

    runtime.start()
    upload_ftp = FTP()
    admin_ftp = FTP()
    try:
        assert runtime.port is not None

        upload_ftp.connect("127.0.0.1", runtime.port, timeout=5)
        upload_ftp.login("nco_ftp", "uploadcaps0731!")
        upload_ftp.storbinary("STOR solve.mp4", io.BytesIO(b"abc"))
        upload_ftp.storbinary("APPE solve.mp4", io.BytesIO(b"def"))

        with pytest.raises(error_perm):
            upload_ftp.retrbinary("RETR solve.mp4", lambda _: None)

        admin_ftp.connect("127.0.0.1", runtime.port, timeout=5)
        admin_ftp.login("admin", "nervergonnagiveyouup")
        assert admin_ftp.nlst() == ["solve.mp4"]

        downloaded = bytearray()
        admin_ftp.retrbinary("RETR solve.mp4", downloaded.extend)
        assert bytes(downloaded) == b"abcdef"

        admin_ftp.rename("solve.mp4", "renamed.mp4")
        assert admin_ftp.nlst() == ["renamed.mp4"]

        admin_ftp.delete("renamed.mp4")
        assert admin_ftp.nlst() == []
    finally:
        for client in (admin_ftp, upload_ftp):
            try:
                client.close()
            except Exception:
                pass
        runtime.stop()


def test_caps_ftp_runtime_rejects_invalid_credentials(tmp_path) -> None:
    storage_root = tmp_path / "caps"
    storage_root.mkdir()

    runtime = CapsFTPServerRuntime(
        host="127.0.0.1",
        port=0,
        username="nco_ftp",
        password="uploadcaps0731!",
        admin_username="admin",
        admin_password="nervergonnagiveyouup",
        storage_root=storage_root,
        passive_ports=tuple(range(42120, 42130)),
    )

    runtime.start()
    ftp = FTP()
    try:
        assert runtime.port is not None
        ftp.connect("127.0.0.1", runtime.port, timeout=5)
        with pytest.raises(error_perm):
            ftp.login("nco_ftp", "wrong-password")
        with pytest.raises(error_perm):
            ftp.login("admin", "wrong-password")
    finally:
        try:
            ftp.close()
        finally:
            runtime.stop()


def write_self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1"),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=7))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = tmp_path / "ftps-cert.pem"
    key_path = tmp_path / "ftps-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_caps_ftps_runtime_supports_explicit_tls_upload_only_permissions(tmp_path) -> None:
    storage_root = tmp_path / "caps"
    storage_root.mkdir()
    cert_path, key_path = write_self_signed_cert(tmp_path)

    runtime = CapsFTPServerRuntime(
        host="127.0.0.1",
        port=0,
        username="nco_ftp",
        password="uploadcaps0731!",
        admin_username="admin",
        admin_password="nervergonnagiveyouup",
        storage_root=storage_root,
        passive_ports=tuple(range(42130, 42140)),
        tls_enabled=True,
        tls_certfile=str(cert_path),
        tls_keyfile=str(key_path),
        tls_control_required=True,
        tls_data_required=True,
    )

    runtime.start()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    ftps = FTP_TLS(context=context)
    try:
        assert runtime.port is not None
        ftps.connect("127.0.0.1", runtime.port, timeout=5)
        ftps.login("nco_ftp", "uploadcaps0731!")
        assert ftps.pwd() == "/"

        with pytest.raises(error_perm, match="SSL/TLS required on the data channel"):
            ftps.nlst()

        ftps.prot_p()
        ftps.storbinary("STOR solve.mp4", io.BytesIO(b"abc"))
        ftps.storbinary("APPE solve.mp4", io.BytesIO(b"def"))
        assert ftps.nlst() == ["solve.mp4"]

        stored_path = storage_root / "solve.mp4"
        assert stored_path.read_bytes() == b"abcdef"

        with pytest.raises(error_perm):
            ftps.retrbinary("RETR solve.mp4", lambda _: None)
        with pytest.raises(error_perm):
            ftps.delete("solve.mp4")
        with pytest.raises(error_perm):
            ftps.rename("solve.mp4", "renamed.mp4")
    finally:
        try:
            ftps.close()
        finally:
            runtime.stop()


def test_normalize_ftp_public_host_accepts_ipv4() -> None:
    assert normalize_ftp_public_host("202.112.51.237") == "202.112.51.237"


def test_normalize_ftp_public_host_resolves_domain_to_ipv4(monkeypatch) -> None:
    monkeypatch.setattr(
        "caps.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("202.112.51.237", 0)),
        ],
    )

    assert normalize_ftp_public_host("ncoai.zephyr369.online") == "202.112.51.237"

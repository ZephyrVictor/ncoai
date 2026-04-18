from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException


SUPPORTED_NOTIFICATION_CHANNEL_TYPES = ("smtp_email", "webhook")
SMTP_SECURITY_VALUES = {"none", "starttls", "ssl"}


@dataclass(frozen=True, slots=True)
class SecretCipher:
    fernet: Fernet | None
    error: str | None = None

    @property
    def configured(self) -> bool:
        return self.fernet is not None

    def encrypt_mapping(self, payload: dict[str, Any]) -> str | None:
        if not payload:
            return None
        if self.fernet is None:
            raise ValueError(self.error or "ADMIN_MONITORING_SECRET_KEY is not configured")
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return self.fernet.encrypt(serialized.encode("utf-8")).decode("utf-8")

    def decrypt_mapping(self, ciphertext: str | None) -> dict[str, Any]:
        if not ciphertext:
            return {}
        if self.fernet is None:
            raise ValueError(self.error or "ADMIN_MONITORING_SECRET_KEY is not configured")
        try:
            raw = self.fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("Stored monitor secret cannot be decrypted") from exc
        parsed = json.loads(raw)
        return dict(parsed or {})


def build_secret_cipher(secret_key: str) -> SecretCipher:
    normalized = str(secret_key or "").strip()
    if not normalized:
        return SecretCipher(None, "ADMIN_MONITORING_SECRET_KEY is not configured")
    try:
        return SecretCipher(Fernet(normalized.encode("utf-8")), None)
    except Exception:
        return SecretCipher(None, "ADMIN_MONITORING_SECRET_KEY is invalid")


def parse_email_list(value: Any) -> list[str]:
    if isinstance(value, list):
        values = value
    else:
        values = str(value or "").split(",")
    emails = [str(item).strip() for item in values if str(item).strip()]
    if not emails:
        raise HTTPException(status_code=400, detail="at least one recipient email is required")
    return emails


def _normalize_smtp_public(config_public: dict[str, Any]) -> dict[str, Any]:
    host = str(config_public.get("host") or "").strip()
    if not host:
        raise HTTPException(status_code=400, detail="SMTP host is required")
    from_email = str(config_public.get("from_email") or "").strip()
    if not from_email:
        raise HTTPException(status_code=400, detail="SMTP from_email is required")
    security = str(config_public.get("security") or "starttls").strip().lower()
    if security not in SMTP_SECURITY_VALUES:
        raise HTTPException(status_code=400, detail="invalid SMTP security mode")
    try:
        port = int(config_public.get("port") or 587)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid SMTP port") from exc
    if port <= 0 or port > 65535:
        raise HTTPException(status_code=400, detail="invalid SMTP port")
    return {
        "host": host,
        "port": port,
        "username": str(config_public.get("username") or "").strip(),
        "from_email": from_email,
        "to_emails": parse_email_list(config_public.get("to_emails")),
        "security": security,
    }


def _normalize_webhook_public(config_public: dict[str, Any]) -> dict[str, Any]:
    url = str(config_public.get("url") or "").strip()
    parsed = urlparse(url)
    if not url or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Webhook url must be a valid HTTP(S) URL")
    try:
        timeout_seconds = int(config_public.get("timeout_seconds") or 10)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid webhook timeout_seconds") from exc
    if timeout_seconds <= 0 or timeout_seconds > 300:
        raise HTTPException(status_code=400, detail="invalid webhook timeout_seconds")
    return {
        "url": url,
        "timeout_seconds": timeout_seconds,
    }


def normalize_notification_channel_payload(
    *,
    name: str,
    channel_type: str,
    is_active: bool,
    config_public: dict[str, Any] | None,
    config_secret: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise HTTPException(status_code=400, detail="channel name is required")

    normalized_type = str(channel_type or "").strip().lower()
    if normalized_type not in SUPPORTED_NOTIFICATION_CHANNEL_TYPES:
        raise HTTPException(status_code=400, detail="unsupported channel_type")

    public = dict(config_public or {})
    secret = dict(config_secret or {})
    if normalized_type == "smtp_email":
        return {
            "name": normalized_name,
            "channel_type": normalized_type,
            "is_active": bool(is_active),
            "config_public": _normalize_smtp_public(public),
            "config_secret": {
                "smtp_password": str(secret.get("smtp_password") or "").strip(),
            },
        }

    return {
        "name": normalized_name,
        "channel_type": normalized_type,
        "is_active": bool(is_active),
        "config_public": _normalize_webhook_public(public),
        "config_secret": {
            "bearer_token": str(secret.get("bearer_token") or "").strip(),
        },
    }


def merge_channel_secret_ciphertext(
    *,
    normalized_payload: dict[str, Any],
    existing_channel: dict[str, Any] | None,
    secret_cipher: SecretCipher,
) -> str | None:
    new_secret = {
        key: value
        for key, value in dict(normalized_payload.get("config_secret") or {}).items()
        if str(value or "").strip()
    }
    if new_secret:
        try:
            return secret_cipher.encrypt_mapping(new_secret)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if (
        existing_channel
        and existing_channel.get("has_secret")
        and str(existing_channel.get("channel_type") or "").strip().lower()
        == str(normalized_payload.get("channel_type") or "").strip().lower()
    ):
        return str(existing_channel.get("config_secret_ciphertext") or "")
    return None


def decrypt_channel_secret(
    channel: dict[str, Any],
    *,
    secret_cipher: SecretCipher,
) -> dict[str, Any]:
    return secret_cipher.decrypt_mapping(
        str(channel.get("config_secret_ciphertext") or "").strip() or None
    )


def _send_smtp_message(
    channel: dict[str, Any],
    secret: dict[str, Any],
    *,
    subject: str,
    body: str,
) -> None:
    config = dict(channel.get("config_public") or {})
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["from_email"]
    message["To"] = ", ".join(config["to_emails"])
    message.set_content(body)

    security = str(config.get("security") or "starttls")
    password = str(secret.get("smtp_password") or "").strip()
    username = str(config.get("username") or "").strip()
    timeout_seconds = 15
    if security == "ssl":
        with smtplib.SMTP_SSL(
            config["host"],
            int(config["port"]),
            timeout=timeout_seconds,
            context=ssl.create_default_context(),
        ) as client:
            if username and password:
                client.login(username, password)
            client.send_message(message)
        return

    with smtplib.SMTP(config["host"], int(config["port"]), timeout=timeout_seconds) as client:
        client.ehlo()
        if security == "starttls":
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if username and password:
            client.login(username, password)
        client.send_message(message)


def _send_webhook_payload(
    channel: dict[str, Any],
    secret: dict[str, Any],
    *,
    payload: dict[str, Any],
) -> None:
    config = dict(channel.get("config_public") or {})
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    bearer_token = str(secret.get("bearer_token") or "").strip()
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = Request(
        config["url"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=int(config.get("timeout_seconds") or 10)) as response:
            response.read()
            if int(response.status) >= 400:
                raise RuntimeError(f"Webhook returned HTTP {response.status}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Webhook returned HTTP {exc.code}: {detail}".strip()) from exc
    except URLError as exc:
        raise RuntimeError(f"Webhook request failed: {exc.reason}") from exc


async def dispatch_notification_to_channel(
    channel: dict[str, Any],
    *,
    subject: str,
    body: str,
    payload: dict[str, Any],
    secret_cipher: SecretCipher,
) -> dict[str, Any]:
    try:
        secret = decrypt_channel_secret(channel, secret_cipher=secret_cipher)
        if channel["channel_type"] == "smtp_email":
            await asyncio.to_thread(
                _send_smtp_message,
                channel,
                secret,
                subject=subject,
                body=body,
            )
        elif channel["channel_type"] == "webhook":
            await asyncio.to_thread(
                _send_webhook_payload,
                channel,
                secret,
                payload=payload,
            )
        else:
            raise RuntimeError(f"Unsupported channel_type: {channel['channel_type']}")
    except Exception as exc:
        return {
            "channel_id": channel["id"],
            "name": channel["name"],
            "channel_type": channel["channel_type"],
            "ok": False,
            "detail": str(exc),
        }

    return {
        "channel_id": channel["id"],
        "name": channel["name"],
        "channel_type": channel["channel_type"],
        "ok": True,
        "detail": "sent",
    }


async def send_test_notification(
    channel: dict[str, Any],
    *,
    secret_cipher: SecretCipher,
) -> dict[str, Any]:
    subject = f"[NCOAI Monitor] Test notification: {channel['name']}"
    payload = {
        "event_type": "test",
        "channel_id": channel["id"],
        "channel_name": channel["name"],
        "channel_type": channel["channel_type"],
        "message": "This is a test notification from NCOAI admin monitoring.",
    }
    body = (
        "NCOAI admin monitoring test notification.\n\n"
        f"Channel: {channel['name']} ({channel['channel_type']})\n"
        "If you received this, the delivery path is working."
    )
    return await dispatch_notification_to_channel(
        channel,
        subject=subject,
        body=body,
        payload=payload,
        secret_cipher=secret_cipher,
    )

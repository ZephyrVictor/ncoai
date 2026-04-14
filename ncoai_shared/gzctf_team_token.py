from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


TEAM_TOKEN_MESSAGE_PREFIX = "GZCTF_TEAM_"


@dataclass(frozen=True, slots=True)
class ParsedTeamToken:
    team_id: int
    signature: str


def decode_base64_data(
    value: str,
    *,
    expected_length: int | None = None,
    field_name: str = "value",
) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field_name} must be valid base64") from exc

    if expected_length is not None and len(decoded) != expected_length:
        raise ValueError(
            f"{field_name} must decode to exactly {expected_length} bytes"
        )

    return decoded


def parse_team_token(team_token: str) -> ParsedTeamToken:
    token = team_token.strip()
    if not token:
        raise ValueError("team token is required")

    team_id_text, separator, signature = token.partition(":")
    if not separator:
        raise ValueError("team token must contain ':'")

    if not signature.strip():
        raise ValueError("team token signature is required")

    try:
        team_id = int(team_id_text.strip())
    except ValueError as exc:
        raise ValueError("team token team id must be an integer") from exc

    if team_id <= 0:
        raise ValueError("team token team id must be positive")

    return ParsedTeamToken(team_id=team_id, signature=signature.strip())


def team_token_message(team_id: int) -> bytes:
    if team_id <= 0:
        raise ValueError("team id must be positive")
    return f"{TEAM_TOKEN_MESSAGE_PREFIX}{team_id}".encode("utf-8")


def derive_public_key_base64_from_private_key(private_key_base64: str) -> str:
    private_key_bytes = decode_base64_data(
        private_key_base64,
        expected_length=32,
        field_name="private key",
    )
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    public_key = private_key.public_key()
    return base64.b64encode(
        public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")


def resolve_public_key_base64(
    public_key_base64: str,
    private_key_base64: str,
) -> str:
    public_key = public_key_base64.strip()
    private_key = private_key_base64.strip()

    if public_key:
        decode_base64_data(
            public_key,
            expected_length=32,
            field_name="public key",
        )

    if private_key:
        derived_public_key = derive_public_key_base64_from_private_key(private_key)
        if public_key and public_key != derived_public_key:
            raise ValueError("configured public key does not match private key")
        return derived_public_key

    if public_key:
        return public_key

    raise ValueError("either public key or private key must be configured")


def verify_gzctf_team_token(
    team_id: int,
    signature: str,
    public_key_base64: str,
) -> bool:
    public_key_bytes = decode_base64_data(
        public_key_base64,
        expected_length=32,
        field_name="public key",
    )
    signature_bytes = decode_base64_data(
        signature,
        field_name="signature",
    )
    public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)

    try:
        public_key.verify(signature_bytes, team_token_message(team_id))
    except InvalidSignature:
        return False

    return True

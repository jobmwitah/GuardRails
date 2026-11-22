"""Shared helpers for the cTrader OAuth flow.

The backend owns the OAuth grant so a refresh token is rotated and persisted in
one place. Access tokens live for ~30 days; refresh tokens have no expiry of
their own but rotate on every refresh, so callers must treat a refresh response
as authoritative and store both new tokens atomically.

The Fernet helpers also protect the short-lived OAuth state/token blobs. Nothing
here logs or returns a client secret or an OAuth token.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken


def _clean_url(value: str) -> str:
    return (value or "").strip().rstrip("/")


PUBLIC_BASE_URL = _clean_url(os.environ.get("PUBLIC_BASE_URL", ""))
PROVISION_TOKEN_TTL = int(os.environ.get("PROVISION_TOKEN_TTL", "900") or "900")


class ProvisioningError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = os.environ.get("CREDENTIAL_KEY", "").strip()
    if not key:
        raise ProvisioningError("CREDENTIAL_KEY is not configured on the backend")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ProvisioningError("CREDENTIAL_KEY is not a valid Fernet key") from exc


def encrypt_text(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ProvisioningError("could not decrypt encrypted payload") from exc


def encrypt_json(value: dict) -> str:
    return encrypt_text(json.dumps(value, separators=(",", ":"), sort_keys=True))


def decrypt_json(value: str) -> dict:
    return json.loads(decrypt_text(value))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_bridge_token(token: str) -> str:
    return sha256_hex(token)


def bridge_token_matches(token: str, expected_hash: str) -> bool:
    if not token or not expected_hash:
        return False
    return hmac.compare_digest(hash_bridge_token(token), expected_hash)


def message_html(title: str, message: str, ok: bool = True) -> str:
    color = "#22c55e" if ok else "#f87171"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ margin:0; background:#0f172a; color:#e2e8f0; font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif; }}
  .wrap {{ max-width:520px; margin:0 auto; padding:56px 20px; }}
  .card {{ background:#1e293b; border:1px solid #334155; border-radius:14px; padding:26px; }}
  h1 {{ color:{color}; font-size:22px; margin:0 0 10px; }}
  p {{ color:#cbd5e1; }}
</style>
</head>
<body><div class="wrap"><div class="card">
<h1>{html.escape(title)}</h1><p>{message}</p>
</div></div></body>
</html>"""

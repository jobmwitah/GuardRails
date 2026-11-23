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
<meta name="referrer" content="no-referrer">
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


def mt5_setup_form_html(login: str, server: str, error: str = "",
                        broker_url: str = "") -> str:
    """One-time MT5 credential form. The password is never stored by the app."""
    error_html = (
        f'<p style="color:#f87171;margin:0 0 14px">{html.escape(error)}</p>'
        if error
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex,nofollow">
<title>GuardRails · MT5 setup</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; background:radial-gradient(1200px 600px at 20% -10%,#1e3a5f 0%,#0f172a 45%,#0b1120 100%); color:#e2e8f0; font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif; }}
  .wrap {{ max-width:480px; margin:0 auto; padding:44px 18px; }}
  .card {{ background:rgba(30,41,59,.78); border:1px solid rgba(148,163,184,.22); border-radius:20px; padding:26px; box-shadow:0 24px 60px rgba(0,0,0,.35); backdrop-filter:blur(18px); }}
  h1 {{ font-size:21px; margin:0 0 6px; letter-spacing:-.02em; }}
  .sub {{ margin:0 0 22px; color:#94a3b8; font-size:13px; }}
  .login {{ display:flex; align-items:center; justify-content:space-between; padding:10px 12px; margin-bottom:16px; border:1px solid rgba(148,163,184,.22); border-radius:12px; background:rgba(15,23,42,.55); font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }}
  .login span {{ color:#94a3b8; font-family:system-ui,sans-serif; font-size:12px; }}
  label {{ display:block; margin:0 0 14px; font-size:12px; font-weight:650; letter-spacing:.04em; color:#cbd5e1; text-transform:uppercase; }}
  input {{ width:100%; margin-top:6px; padding:12px 13px; border-radius:12px; border:1px solid rgba(148,163,184,.28); background:rgba(15,23,42,.72); color:#f8fafc; font:15px/1.3 system-ui,sans-serif; outline:none; }}
  input:focus {{ border-color:#38bdf8; box-shadow:0 0 0 3px rgba(56,189,248,.15); }}
  .note {{ font-size:12px; line-height:1.5; color:#94a3b8; margin:6px 0 18px; }}
  .warn {{ color:#fbbf24; }}
  button {{ width:100%; padding:13px 16px; border:0; border-radius:12px; background:linear-gradient(135deg,#38bdf8,#6366f1); color:#fff; font-size:15px; font-weight:700; cursor:pointer; }}
  button:active {{ transform:translateY(1px); }}
</style>
</head>
<body><div class="wrap"><div class="card">
<h1>Connect your MT5 account</h1>
<p class="sub">GuardRails will create an isolated terminal on the VPS.</p>
{error_html}
<div class="login"><span>Login</span><strong>{html.escape(login)}</strong></div>
<form method="post" action="" autocomplete="off">
  <label>Broker server
    <input name="server" value="{html.escape(server)}" maxlength="120" required spellcheck="false">
  </label>
  <label>MT5 trading password
    <input name="password" type="password" maxlength="128" required autocomplete="new-password" spellcheck="false">
  </label>
  <label>Broker MT5 download link <span style="text-transform:none;font-weight:400;color:#94a3b8">(optional)</span>
    <input name="broker_url" value="{html.escape(broker_url)}" maxlength="500" placeholder="https://download.mql5.com/...exe" spellcheck="false">
  </label>
  <p class="note">Use the <strong>trading password</strong>, not the investor password.
  It is sent over HTTPS, written to this one terminal for the first login, then
  removed. GuardRails never stores it in the database.</p>
  <p class="note warn">The server name must match MT5 exactly, for example
  <code>Pepperstone-Demo</code> or <code>FundingPips-Trial</code>.
  If your server is not recognized, paste your broker's official MT5
  <strong>.exe</strong> download link above so GuardRails can cache its server
  list. Never paste the link to a password manager or file share.</p>
  <button type="submit">Start terminal</button>
</form>
</div></div></body>
</html>"""


def mt5_setup_status_html(title: str, message: str, ok: bool = True,
                          refresh_seconds: int = 0) -> str:
    refresh = (
        f'<meta http-equiv="refresh" content="{int(refresh_seconds)}">'
        if refresh_seconds > 0
        else ""
    )
    color = "#22c55e" if ok else "#f87171"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta name="robots" content="noindex,nofollow">
{refresh}
<title>GuardRails · MT5 setup</title>
<style>
  body {{ margin:0; min-height:100vh; background:#0b1120; color:#e2e8f0; font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif; }}
  .wrap {{ max-width:480px; margin:0 auto; padding:56px 18px; }}
  .card {{ background:#1e293b; border:1px solid #334155; border-radius:18px; padding:26px; }}
  h1 {{ color:{color}; font-size:21px; margin:0 0 10px; }}
  p {{ color:#cbd5e1; margin:0; }}
  .small {{ margin-top:14px; color:#94a3b8; font-size:12px; }}
</style>
</head>
<body><div class="wrap"><div class="card">
<h1>{html.escape(title)}</h1>
<p>{message}</p>
<p class="small">You can close this tab once the account shows live in GuardRails.</p>
</div></div></body>
</html>"""

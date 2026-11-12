"""Secure account-provisioning helpers.

The Discord command never receives an MT5 password. It creates a single-use
link; the owner opens the link, enters the trading password over HTTPS, and
the backend encrypts it before queueing a job for the Windows agent.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken

import storage as db


PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
PROVISION_TOKEN_TTL = int(os.environ.get("PROVISION_TOKEN_TTL", "900") or "900")
AGENT_STALE_SECONDS = int(os.environ.get("AGENT_STALE_SECONDS", "90") or "90")


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
        raise ProvisioningError("could not decrypt provisioning payload") from exc


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


def agent_token_ok(supplied: str | None) -> bool:
    expected = os.environ.get("AGENT_TOKEN", "").strip()
    if not expected or not supplied:
        return False
    return hmac.compare_digest(str(supplied).encode("utf-8"), expected.encode("utf-8"))


def agent_is_online(agent_id: str = "windows-1") -> bool:
    row = db.get_agent_state(agent_id)
    if not row:
        return False
    return (time.time() - float(row.get("last_seen") or 0)) <= AGENT_STALE_SECONDS


def agent_summary(agent_id: str = "windows-1") -> str:
    row = db.get_agent_state(agent_id)
    if not row:
        return "Windows provisioning agent: **never connected**."
    age = int(time.time() - float(row.get("last_seen") or 0))
    state = "online" if age <= AGENT_STALE_SECONDS else "offline"
    stats = _agent_stats(row)
    bits = [f"Windows provisioning agent: **{state}** (last heartbeat {age}s ago)"]
    if stats:
        bits.append(
            f"instances {stats.get('running', '?')} running / {stats.get('total', '?')} total; "
            f"RAM free {stats.get('ram_free_mb', '?')} MB of {stats.get('ram_total_mb', '?')} MB"
        )
    if row.get("hostname"):
        bits.append(f"host `{row['hostname']}`")
    return ". ".join(bits) + "."


def _agent_stats(row: dict) -> dict:
    try:
        stats = json.loads(row.get("stats") or "{}")
        return stats if isinstance(stats, dict) else {}
    except (TypeError, ValueError):
        return {}


def agent_instances(agent_id: str = "windows-1") -> dict[str, dict]:
    """Live instance map reported by the Windows agent's last heartbeat."""
    row = db.get_agent_state(agent_id)
    if not row:
        return {}
    stats = _agent_stats(row)
    out: dict[str, dict] = {}
    for item in stats.get("instances") or []:
        if not isinstance(item, dict):
            continue
        login = str(item.get("login") or "")
        if not login:
            continue
        out[login] = item
    return out


def agent_online(agent_id: str = "windows-1") -> bool:
    row = db.get_agent_state(agent_id)
    if not row:
        return False
    return (time.time() - float(row.get("last_seen") or 0)) <= AGENT_STALE_SECONDS


def create_provision_link(login: str) -> str:
    raw = new_secret()
    db.create_provision_token(sha256_hex(raw), login, time.time() + PROVISION_TOKEN_TTL)
    base = PUBLIC_BASE_URL
    if not base:
        raise ProvisioningError("PUBLIC_BASE_URL is not configured on the backend")
    return f"{base}/provision/{raw}"


def form_response_html(login: str, server: str, error: str = "") -> str:
    login_esc = html.escape(login)
    server_esc = html.escape(server)
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GuardRails account setup</title>
<style>
  body {{ margin:0; background:#0f172a; color:#e2e8f0; font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }}
  .wrap {{ max-width:480px; margin:0 auto; padding:48px 20px; }}
  .card {{ background:#1e293b; border:1px solid #334155; border-radius:14px; padding:24px; }}
  h1 {{ font-size:22px; margin:0 0 6px; }}
  p {{ color:#94a3b8; }}
  label {{ display:block; margin:18px 0 6px; color:#cbd5e1; font-weight:600; }}
  input {{ width:100%; box-sizing:border-box; padding:11px 12px; border-radius:8px; border:1px solid #475569; background:#0f172a; color:#f8fafc; font-size:16px; }}
  input[readonly] {{ color:#94a3b8; }}
  button {{ margin-top:22px; width:100%; padding:12px; border:0; border-radius:8px; background:#22c55e; color:#052e16; font-weight:700; font-size:16px; cursor:pointer; }}
  .error {{ color:#fca5a5; background:#450a0a; border:1px solid #7f1d1d; padding:10px; border-radius:8px; }}
  .note {{ font-size:13px; color:#64748b; margin-top:18px; }}
</style>
</head>
<body>
<div class="wrap"><div class="card">
  <h1>GuardRails account setup</h1>
  <p>Enter the MT5 trading password for <strong>{login_esc}</strong>. The password is encrypted immediately and is never posted to Discord.</p>
  {error_html}
  <form method="post" autocomplete="off">
    <label>MT5 login</label>
    <input value="{login_esc}" readonly>
    <label for="server">Server</label>
    <input id="server" name="server" value="{server_esc}" required>
    <label for="password">MT5 trading password</label>
    <input id="password" name="password" type="password" required autofocus>
    <button type="submit">Provision this account</button>
  </form>
  <p class="note">This link is single-use and expires in {PROVISION_TOKEN_TTL // 60} minutes. Use the master/trading password, not the investor password. Accounts with 2FA cannot be automated.</p>
</div></div>
</body>
</html>"""


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

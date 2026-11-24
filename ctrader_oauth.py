"""cTrader Open API OAuth helpers.

The backend owns the OAuth grant so a refresh token is rotated and persisted in
one place. cTrader access tokens live for ~30 days; refresh tokens have no
expiry of their own but rotate on every refresh, so callers must treat a refresh
response as authoritative and store both new tokens atomically.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

AUTHORIZE_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
TOKEN_URL = "https://openapi.ctrader.com/apps/token"
DEFAULT_EXPIRES_IN = 2628000  # cTrader's documented default (30.4 days)
REQUEST_TIMEOUT = 20

# Never put these values in a log line or a Discord message.
CLIENT_ID = os.environ.get("CTRADER_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("CTRADER_CLIENT_SECRET", "").strip()
REDIRECT_URI = os.environ.get("CTRADER_REDIRECT_URI", "").strip()


class OAuthError(RuntimeError):
    """A safe-to-display OAuth failure. Never contains client secrets/tokens."""


class OAuthTransientError(OAuthError):
    """A temporary network/server failure; the grant should not be revoked."""


def public_base_url() -> str:
    # Imported lazily so this module stays usable in tests without the backend.
    from provisioning import PUBLIC_BASE_URL

    return PUBLIC_BASE_URL.rstrip("/")


def redirect_uri() -> str:
    return REDIRECT_URI or (f"{public_base_url()}/ctrader/callback" if public_base_url() else "")


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and redirect_uri())


def authorization_url(state: str) -> str:
    if not configured():
        raise OAuthError(
            "cTrader OAuth is not configured on the backend. Set CTRADER_CLIENT_ID, "
            "CTRADER_CLIENT_SECRET and CTRADER_REDIRECT_URI."
        )
    query = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "scope": "trading",
        "product": "web",
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{query}"


def _token_request(params: dict, *, method: str = "GET") -> dict:
    url = f"{TOKEN_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # Read the API's error payload, but never echo the request URL (it
        # contains the authorization code / refresh token).
        detail = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            detail = str(payload.get("description") or payload.get("errorCode") or "")
        except Exception:
            pass
        message = (
            f"cTrader token endpoint rejected the request (HTTP {exc.code})"
            + (f": {detail}" if detail else ".")
        )
        if exc.code >= 500 or exc.code == 429:
            raise OAuthTransientError(message) from None
        raise OAuthError(message) from None
    except urllib.error.URLError as exc:
        raise OAuthTransientError(
            f"Could not reach the cTrader token endpoint: {exc.reason}"
        ) from None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise OAuthTransientError("cTrader returned an unreadable token response.") from None

    access = payload.get("accessToken")
    refresh = payload.get("refreshToken")
    if not access or not refresh:
        detail = payload.get("description") or payload.get("errorCode") or "missing tokens"
        raise OAuthError(f"cTrader did not return usable tokens: {detail}")

    try:
        expires_in = int(payload.get("expiresIn") or DEFAULT_EXPIRES_IN)
    except (TypeError, ValueError):
        expires_in = DEFAULT_EXPIRES_IN

    return {
        "access_token": str(access),
        "refresh_token": str(refresh),
        "expires_in": max(60, expires_in),
        "token_type": str(payload.get("tokenType") or "bearer"),
    }


def exchange_code(code: str) -> dict:
    if not configured():
        raise OAuthError("cTrader OAuth is not configured on the backend.")
    if not code:
        raise OAuthError("cTrader did not return an authorization code.")
    return _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })


def refresh(refresh_token: str) -> dict:
    if not configured():
        raise OAuthError("cTrader OAuth is not configured on the backend.")
    if not refresh_token:
        raise OAuthError("No cTrader refresh token is stored for this grant.")
    return _token_request({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }, method="POST")

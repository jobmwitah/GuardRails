"""HTTP client for the GuardRails backend.

The connector deliberately uses the same HTTP contract as the MT5 EA
(`/report`, `/commands`, `/event`) plus a small token-gated cTrader API.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request


class BackendError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"backend HTTP {status}: {message}")


class BackendAuthRequired(BackendError):
    pass


class BackendClient:
    def __init__(self, base_url: str, connector_token: str, *, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.connector_token = connector_token
        self.timeout = timeout

    def _sync_request(self, method: str, path: str, *, body: bytes | None = None,
                      headers: dict | None = None) -> tuple[int, str]:
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=body, headers=headers or {}, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise BackendError(exc.code, detail or exc.reason) from None
        except urllib.error.URLError as exc:
            raise BackendError(0, f"backend unreachable: {exc.reason}") from None

    async def _request(self, method: str, path: str, *, body: bytes | None = None,
                       headers: dict | None = None) -> str:
        status, text = await asyncio.to_thread(
            self._sync_request, method, path, body=body, headers=headers
        )
        if status >= 400:
            raise BackendError(status, text[:500])
        return text

    async def _get_json(self, path: str) -> dict:
        text = await self._request(
            "GET", path, headers={"X-Connector-Token": self.connector_token}
        )
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise BackendError(200, "backend returned invalid JSON") from None
        if not isinstance(value, dict):
            raise BackendError(200, "backend returned an unexpected JSON shape")
        return value

    async def _post_json(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        text = await self._request(
            "POST",
            path,
            body=body,
            headers={
                "X-Connector-Token": self.connector_token,
                "Content-Type": "application/json",
            },
        )
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": True}
        return value if isinstance(value, dict) else {"ok": True}

    async def get_grants(self) -> list[dict]:
        data = await self._get_json("/ctrader/grants")
        grants = data.get("grants")
        return grants if isinstance(grants, list) else []

    async def get_session(self, grant_id: int) -> dict:
        try:
            return await self._get_json(f"/ctrader/grants/{int(grant_id)}/session")
        except BackendError as exc:
            if exc.status == 409:
                raise BackendAuthRequired(exc.status, exc.message) from None
            raise

    async def post_grant_accounts(self, grant_id: int, accounts: list[dict]) -> dict:
        return await self._post_json(
            f"/ctrader/grants/{int(grant_id)}/accounts", {"accounts": accounts}
        )

    async def post_grant_status(self, grant_id: int, status: str, error: str = "") -> None:
        await self._post_json(
            f"/ctrader/grants/{int(grant_id)}/status",
            {"status": status, "error": error[:500]},
        )

    async def get_accounts(self) -> list[dict]:
        data = await self._get_json("/ctrader/accounts")
        accounts = data.get("accounts")
        return accounts if isinstance(accounts, list) else []

    async def get_commands(self, login: str) -> list[str]:
        query = urllib.parse.urlencode({"token": self.connector_token, "account": login})
        text = await self._request("GET", f"/commands?{query}")
        return [line.strip() for line in text.splitlines() if line.strip()]

    async def post_report(self, login: str, fields: dict) -> None:
        data = {"token": self.connector_token, "account": login, **fields}
        body = urllib.parse.urlencode(data).encode("utf-8")
        await self._request(
            "POST",
            "/report",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    async def post_candles(self, login: str, symbol: str, timeframe: str, bars: str) -> None:
        data = {
            "token": self.connector_token,
            "account": login,
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": bars,
        }
        body = urllib.parse.urlencode(data).encode("utf-8")
        await self._request(
            "POST",
            "/candles",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    async def post_event(self, login: str, message: str) -> None:
        body = urllib.parse.urlencode({
            "token": self.connector_token,
            "account": login,
            "message": message,
        }).encode("utf-8")
        await self._request(
            "POST",
            "/event",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

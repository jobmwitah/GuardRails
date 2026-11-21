"""Connector configuration from the environment.

The connector runs next to the backend on the VPS. It reads the same `.env`
file, but only needs the cTrader app credentials and the shared connector
secret; OAuth tokens stay in the backend's encrypted storage.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # convenient when running from a checkout; systemd passes real env vars
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass


class ConfigError(RuntimeError):
    pass


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _aliases() -> dict[str, str]:
    raw = os.environ.get("CTRADER_SYMBOL_ALIASES", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "CTRADER_SYMBOL_ALIASES must be a JSON object, e.g. "
            '{"US100": "US100.cash", "XAUUSD": "GOLD"}'
        ) from exc
    if not isinstance(value, dict):
        raise ConfigError("CTRADER_SYMBOL_ALIASES must be a JSON object")
    return {str(k): str(v) for k, v in value.items() if str(k).strip() and str(v).strip()}


@dataclass(frozen=True)
class Settings:
    backend_url: str
    connector_token: str
    client_id: str
    client_secret: str
    demo_host: str = "demo.ctraderapi.com"
    live_host: str = "live.ctraderapi.com"
    port: int = 5036
    report_interval: float = 2.0
    command_interval: float = 1.0
    reconcile_interval: float = 15.0
    discovery_interval: float = 10.0
    request_timeout: float = 12.0
    connect_timeout: float = 15.0
    price_tolerance_points: int = 10
    margin_cap_pct: float = 80.0
    state_dir: Path = field(default_factory=lambda: Path("ctrader_connector/state"))
    symbol_aliases: dict[str, str] = field(default_factory=dict)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        backend_url = os.environ.get("CTRADER_BACKEND_URL", "http://127.0.0.1:8000").strip()
        token = os.environ.get("CTRADER_CONNECTOR_TOKEN", "").strip()
        client_id = os.environ.get("CTRADER_CLIENT_ID", "").strip()
        client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "").strip()
        missing = [
            name for name, value in (
                ("CTRADER_CONNECTOR_TOKEN", token),
                ("CTRADER_CLIENT_ID", client_id),
                ("CTRADER_CLIENT_SECRET", client_secret),
            ) if not value
        ]
        if missing:
            raise ConfigError("missing connector environment variables: " + ", ".join(missing))
        if not backend_url.startswith(("http://", "https://")):
            raise ConfigError("CTRADER_BACKEND_URL must start with http:// or https://")

        return cls(
            backend_url=backend_url.rstrip("/"),
            connector_token=token,
            client_id=client_id,
            client_secret=client_secret,
            demo_host=os.environ.get("CTRADER_DEMO_HOST", "demo.ctraderapi.com").strip(),
            live_host=os.environ.get("CTRADER_LIVE_HOST", "live.ctraderapi.com").strip(),
            port=_int("CTRADER_PORT", 5036),
            report_interval=max(1.0, _float("CTRADER_REPORT_INTERVAL", 2.0)),
            command_interval=max(0.5, _float("CTRADER_COMMAND_INTERVAL", 1.0)),
            reconcile_interval=max(5.0, _float("CTRADER_RECONCILE_INTERVAL", 15.0)),
            discovery_interval=max(5.0, _float("CTRADER_DISCOVERY_INTERVAL", 10.0)),
            request_timeout=max(3.0, _float("CTRADER_REQUEST_TIMEOUT", 12.0)),
            connect_timeout=max(3.0, _float("CTRADER_CONNECT_TIMEOUT", 15.0)),
            price_tolerance_points=max(0, _int("CTRADER_PRICE_TOLERANCE_POINTS", 10)),
            margin_cap_pct=min(100.0, max(0.0, _float("CTRADER_MARGIN_CAP_PCT", 80.0))),
            state_dir=Path(os.environ.get("CTRADER_STATE_DIR", "ctrader_connector/state")),
            symbol_aliases=_aliases(),
            log_level=os.environ.get("CTRADER_LOG_LEVEL", "INFO").upper(),
        )

    def host_for(self, environment: str) -> str:
        return self.live_host if (environment or "").lower() == "live" else self.demo_host

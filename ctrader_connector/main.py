"""Entrypoint for the GuardRails cTrader connector service.

Run from the repository root (or the /opt/guardrails checkout on the VPS):

    python -m ctrader_connector.main

It reads the same `.env` as the backend plus the cTrader-specific variables:

    CTRADER_CONNECTOR_TOKEN
    CTRADER_CLIENT_ID
    CTRADER_CLIENT_SECRET
    CTRADER_BACKEND_URL          (default http://127.0.0.1:8000)
    CTRADER_STATE_DIR            (default ctrader_connector/state)
    CTRADER_SYMBOL_ALIASES       (optional JSON: {"US100": "US100.cash"})

No OAuth token is ever stored by this service; the backend keeps encrypted
grants and hands out short-lived access tokens.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .config import ConfigError, Settings
from .service import ConnectorService

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


async def _run(service: ConnectorService) -> None:
    loop = asyncio.get_running_loop()
    installed = False
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, service.request_stop)
            installed = True
        except (NotImplementedError, RuntimeError, AttributeError):
            # Windows event loops do not support add_signal_handler.
            try:
                signal.signal(sig, lambda *_: service.request_stop())
                installed = True
            except (ValueError, OSError):
                pass
    if not installed:
        logger.warning("no signal handlers installed; stop the service with Ctrl+C")
    await service.run()


def main() -> int:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        configure_logging("INFO")
        logger.error("configuration error: %s", exc)
        return 2
    configure_logging(settings.log_level)
    service = ConnectorService(settings)
    try:
        asyncio.run(_run(service))
    except KeyboardInterrupt:
        logger.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Per-account cTrader Open API worker.

One instance owns one TCP session and one ``ctidTraderAccountId``. It speaks
the same HTTP contract as the MT5 EA:

* ``POST /report``   -- balance/equity/margin, quotes, bot positions and orders
* ``GET  /commands`` -- SYMBOLS/RISK/MAXPOS/RRR plus OPEN/CLOSE/etc.
* ``POST /event``    -- TRADE_CLOSED, POSITION_OPENED, PENDING_TRIGGERED, ...

The cTrader protocol is JSON over TCP (4-byte big-endian length prefix). See
``protocol.py`` and ``json_client.py``.

Safety notes that are easy to lose when editing this file:

* MARKET orders cannot carry SL/TP in cTrader. Protection is amended right
  after the fill and the position is closed immediately if that amend fails.
* Prices are quoted in 1/100000 units and money fields use a per-account
  ``moneyDigits`` exponent. Never assume two decimals.
* Volume is in cents. One lot is ``symbol.lotSize`` protocol volume.
* Only positions/orders whose label starts with ``GuardRails`` belong to us.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import protocol
from .backend import BackendAuthRequired, BackendClient, BackendError
from .json_client import CTraderJSONClient, ConnectionClosed
from .symbols import (
    SizingError,
    SymbolBook,
    conversion_rate,
    fmt_price,
    lots_from_volume,
    money,
    price,
    quote_key,
    round_price,
    size_order,
)

logger = logging.getLogger(__name__)

LABEL = "GuardRails"
LABEL_PREFIX = LABEL
QUOTE_REPORT_MAX_AGE = 180.0   # older than this -> report 0 / market considered closed
QUOTE_ORDER_MAX_AGE = 60.0     # never place an order on a quote this stale
ORDER_FILL_TIMEOUT = 20.0
PROTECTION_TIMEOUT = 12.0
PROTECTION_SWEEP_SECONDS = 60.0
HISTORY_SYNC_SECONDS = 120.0
# Balance/equity/margin only move when cTrader sends a fresh trader snapshot.
# A manual close does not always include one, so re-read it periodically and
# immediately after any fill/close.
TRADER_REFRESH_SECONDS = 10.0
CLOSE_TIMEOUT = 20.0
CANCEL_TIMEOUT = 12.0
DEFAULT_LOT_SIZE = 10_000_000  # cents; cTrader's usual FX lotSize, display fallback only

# ProtoOATrendbarPeriod enum -> approximate milliseconds per bar
TRENDBAR_TIMEFRAMES = {
    "M1": 1, "M5": 5, "M15": 7, "M30": 8, "H1": 9, "H4": 10, "D1": 12,
}
_TRENDBAR_SPAN_MS = {
    1: 60_000, 5: 300_000, 6: 600_000, 7: 900_000, 8: 1_800_000,
    9: 3_600_000, 10: 14_400_000, 11: 43_200_000, 12: 86_400_000,
}


class CommandError(RuntimeError):
    pass


class OrderFailed(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code or "UNKNOWN_ERROR"
        self.detail = detail or ""
        super().__init__(self.code + (f": {self.detail}" if self.detail else ""))


# ---------------- enum helpers (JSON may carry names or numbers) ----------------

_EXECUTION_NAMES = {
    2: "ORDER_ACCEPTED",
    3: "ORDER_FILLED",
    4: "ORDER_REPLACED",
    5: "ORDER_CANCELLED",
    6: "ORDER_EXPIRED",
    7: "ORDER_REJECTED",
    8: "ORDER_CANCEL_REJECTED",
    9: "SWAP",
    10: "DEPOSIT_WITHDRAW",
    11: "ORDER_PARTIAL_FILL",
    12: "BONUS_DEPOSIT_WITHDRAW",
}
_ORDER_STATUS_NAMES = {
    1: "ORDER_STATUS_ACCEPTED",
    2: "ORDER_STATUS_FILLED",
    3: "ORDER_STATUS_REJECTED",
    4: "ORDER_STATUS_EXPIRED",
    5: "ORDER_STATUS_CANCELLED",
    6: "ORDER_STATUS_PARTIALLY_FILLED",
}
_ORDER_TYPE_NAMES = {
    1: "MARKET",
    2: "LIMIT",
    3: "STOP",
    4: "STOP_LOSS_TAKE_PROFIT",
    5: "MARKET_RANGE",
    6: "STOP_LIMIT",
}
_POSITION_STATUS_NAMES = {
    1: "POSITION_STATUS_OPEN",
    2: "POSITION_STATUS_CLOSED",
    3: "POSITION_STATUS_CREATED",
    4: "POSITION_STATUS_ERROR",
}
_SIDE_NAMES = {1: "BUY", 2: "SELL"}
_ACCOUNT_TYPE_NAMES = {0: "HEDGED", 1: "NETTED", 2: "SPREAD_BETTING"}


def enum_name(value, names: dict[int, str], default: str = "") -> str:
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return names.get(int(text), default)
        return text.upper()
    try:
        return names.get(int(value), default)
    except (TypeError, ValueError):
        return default


def execution_name(value) -> str:
    return enum_name(value, _EXECUTION_NAMES, "UNKNOWN")


def order_status_name(value) -> str:
    return enum_name(value, _ORDER_STATUS_NAMES, "UNKNOWN")


def order_type_name(value) -> str:
    return enum_name(value, _ORDER_TYPE_NAMES, "OTHER")


def position_status_name(value) -> str:
    return enum_name(value, _POSITION_STATUS_NAMES, "UNKNOWN")


def side_name(value) -> str:
    return enum_name(value, _SIDE_NAMES, "")


def account_type_name(value) -> str:
    return enum_name(value, _ACCOUNT_TYPE_NAMES, "")


# ---------------- pure helpers (unit-tested without a connection) ----------------

def digits_or(value, fallback: int) -> int:
    if value is None or value == "":
        return fallback
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def int_or(value, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def float_or(value, fallback: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def parse_command(line: str):
    """Return ``(name, args)`` for a backend command, or ``None``.

    Unknown commands are ignored in the same spirit as the MT5 EA: the backend
    may add one for a newer EA without breaking older connectors.
    """
    parts = (line or "").strip().split()
    if not parts:
        return None
    name = parts[0].upper()
    args = parts[1:]
    if name == "SYMBOLS":
        if not args:
            return None
        return name, [" ".join(args)]
    if name in ("RISK", "MAXPOS", "RRR"):
        return (name, args) if args else None
    if name == "CLOSE":
        return (name, args) if len(args) >= 1 else None
    if name == "DELETE_PENDING":
        return (name, args) if len(args) >= 1 else None
    if name == "OPEN":
        return (name, args[:6]) if len(args) >= 6 else None
    if name == "OPEN_MARKET":
        return (name, args[:5]) if len(args) >= 5 else None
    if name == "CANDLES":
        # CANDLES SYMBOL TIMEFRAME COUNT
        return (name, args[:3]) if len(args) >= 3 else None
    if name in ("CLOSE_ALL", "BLOCK", "UNBLOCK"):
        return name, []
    return None


def net_close_profit(detail: dict, fallback_digits: int = 2) -> float:
    """Net deposit-currency P/L of a closing deal.

    cTrader supplies each component as a signed amount (commission/swap fees
    are negative), so summing them matches the balance change. ``moneyDigits``
    is taken from the detail first, then the deal, then the account.
    """
    if not isinstance(detail, dict):
        return 0.0
    digits = digits_or(detail.get("moneyDigits"), fallback_digits)
    return (
        money(detail.get("grossProfit"), digits)
        + money(detail.get("swap"), digits)
        + money(detail.get("commission"), digits)
        + money(detail.get("pnlConversionFee"), digits)
    )


def compute_tp(side: str, entry: float, sl: float, rrr: float) -> float:
    if side == "BUY":
        return entry + (entry - sl) * rrr
    return entry - (sl - entry) * rrr


def valid_protection(side: str, entry: float, sl: float, tp: float) -> bool:
    if entry <= 0 or sl <= 0 or tp <= 0:
        return False
    if side == "BUY":
        return sl < entry < tp
    return tp < entry < sl


def order_plan(side: str, entry: float, ask: float, bid: float, tolerance: float) -> str:
    """Mirror the EA: near-market entries become MARKET, others pending."""
    if side == "BUY":
        if ask <= 0:
            return ""
        if abs(entry - ask) <= tolerance:
            return "MARKET"
        return "LIMIT" if entry < ask else "STOP"
    if bid <= 0:
        return ""
    if abs(entry - bid) <= tolerance:
        return "MARKET"
    return "LIMIT" if entry > bid else "STOP"


def classify_close(deal: dict, position: dict | None, digits: int,
                   initiated_by_bot: bool = False) -> str:
    if initiated_by_bot:
        return "EA"
    price_value = float_or((deal or {}).get("executionPrice"))
    if not position:
        return "MANUAL"
    trade = position.get("tradeData") or {}
    side = side_name(trade.get("tradeSide"))
    sl = float_or(position.get("stopLoss"))
    tp = float_or(position.get("takeProfit"))
    tolerance = max(1e-8, 10 * (10 ** -max(0, digits)))  # ~1 pip at 5 digits
    if price_value <= 0:
        return "MANUAL"
    if side == "BUY":
        if sl and price_value <= sl + tolerance:
            return "SL"
        if tp and price_value >= tp - tolerance:
            return "TP"
    elif side == "SELL":
        if sl and price_value >= sl - tolerance:
            return "SL"
        if tp and price_value <= tp + tolerance:
            return "TP"
    return "MANUAL"


def pending_type_label(order: dict) -> str:
    kind = order_type_name(order.get("orderType"))
    side = side_name((order.get("tradeData") or {}).get("tradeSide"))
    if kind == "LIMIT":
        return "BUY_LIMIT" if side == "BUY" else "SELL_LIMIT"
    if kind == "STOP":
        return "BUY_STOP" if side == "BUY" else "SELL_STOP"
    return "OTHER"


def pending_price(order: dict) -> float:
    kind = order_type_name(order.get("orderType"))
    if kind == "LIMIT":
        return float_or(order.get("limitPrice"))
    if kind == "STOP":
        return float_or(order.get("stopPrice"))
    return 0.0


# ---------------- worker ----------------

@dataclass
class _Watcher:
    predicate: Callable[[dict], bool]
    future: asyncio.Future


class AccountWorker:
    def __init__(self, settings, backend: BackendClient, grant_id: int, account: dict):
        self.settings = settings
        self.backend = backend
        self.grant_id = int(grant_id)
        self.info = dict(account)
        self.login = str(account.get("login") or f"ctrader:{account.get('external_id')}")
        self.ctid = int_or(account.get("external_id"))
        self.environment = str(account.get("environment") or "demo").lower()
        if self.environment not in ("demo", "live"):
            logger.warning(
                "%s: unknown cTrader environment %r; using the demo endpoint",
                self.login, self.environment,
            )
            self.environment = "demo"
        self.account_type = str(account.get("account_type") or "").upper()
        self.symbols = [str(item) for item in (account.get("symbols") or [])]
        self.risk_pct = float_or(account.get("risk_pct"))
        self.max_positions = 0
        self.rrr = 0.0
        self.blocked = False

        self.book = SymbolBook(settings.symbol_aliases)
        self.assets: dict[int, dict] = {}
        self.resolved: dict[str, object] = {}
        self.unavailable_symbols: set[str] = set()
        self.roots_by_id: dict[int, str] = {}
        self.quotes: dict[int, dict] = {}
        self.trader: dict = {}
        self._trader_refreshed_at = 0.0
        self.money_digits = 2
        self.positions: dict[int, dict] = {}
        self.orders: dict[int, dict] = {}
        self.pnl: dict[int, float] = {}
        self.bot_positions: set[int] = set()
        self.bot_orders: set[int] = set()
        self.partial_closes: dict[int, dict] = {}
        self.closed_reported: set[int] = set()
        self.opened_orders: set[int] = set()
        self.order_filled_volume: dict[int, int] = {}
        self.seen_fill_deals: set[int] = set()
        self.entry_backfill_tried: set[int] = set()
        self.trigger_reported: set[int] = set()
        self.closing_intents: dict[int, float] = {}
        self.subscribed: set[int] = set()
        self.wanted_symbol_ids: set[int] = set()
        self.flush_attempts: dict[int, int] = {}

        self.state_path: Path = Path(settings.state_dir) / f"ctrader_{self.ctid}.json"
        self.persisted, loaded_closed = self._load_state()
        self.closed_reported.update(loaded_closed)

        self._client: CTraderJSONClient | None = None
        self._reader: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._restart = asyncio.Event()
        self._watchers: list[_Watcher] = []
        self._symbols_dirty = False
        self.fatal = False

    # ---------- lifecycle ----------

    async def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                session = await self.backend.get_session(self.grant_id)
                await self._connect(str(session["access_token"]))
                failures = 0
                await self._after_connect()
                await self._main_loop()
            except asyncio.CancelledError:
                raise
            except BackendAuthRequired as exc:
                logger.error("%s: cTrader grant needs reauthorization: %s", self.login, exc)
                self.fatal = True
                await self._post_grant_status("reauth", str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - keep one account from killing the service
                failures += 1
                logger.warning("%s: session ended (%s), reconnect %s", self.login, exc, failures)
            finally:
                await self._disconnect()

            if self._stop.is_set():
                return
            if failures >= 2:
                await self._post_grant_status("error", "cTrader session keeps failing; check the backend log")
            await self._sleep_or_stop(min(60.0, 5.0 * max(1, failures)))

    async def stop(self) -> None:
        self._stop.set()
        await self._close_client()

    # ---------- connection / auth ----------

    async def _connect(self, access_token: str) -> None:
        client = CTraderJSONClient(
            self.settings.host_for(self.environment),
            self.settings.port,
            connect_timeout=self.settings.connect_timeout,
            request_timeout=self.settings.request_timeout,
        )
        await client.connect()
        try:
            await client.request(
                protocol.APP_AUTH_REQ,
                {
                    "clientId": self.settings.client_id,
                    "clientSecret": self.settings.client_secret,
                },
                expect=protocol.APP_AUTH_RES,
            )
            await client.request(
                protocol.ACCOUNT_AUTH_REQ,
                {
                    "ctidTraderAccountId": self.ctid,
                    "accessToken": access_token,
                },
                expect=protocol.ACCOUNT_AUTH_RES,
            )
        except Exception:
            await client.close()
            raise

        self._client = client
        self._restart.clear()
        self._watchers.clear()
        self.subscribed.clear()
        self.quotes.clear()
        self._reader = asyncio.create_task(
            self._event_loop(client), name=f"ctrader-events-{self.ctid}"
        )
        logger.info("%s: authenticated cTrader account %s (%s)", self.login, self.ctid, self.environment)

    async def _disconnect(self) -> None:
        await self._close_client()
        reader = self._reader
        self._reader = None
        if reader and not reader.done():
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
        self._fail_watchers(ConnectionClosed("session closed"))
        self.positions.clear()
        self.orders.clear()
        self.pnl.clear()
        self.bot_positions.clear()
        self.bot_orders.clear()
        self.subscribed.clear()
        self.wanted_symbol_ids.clear()
        self.quotes.clear()

    async def _close_client(self) -> None:
        client = self._client
        self._client = None
        if client:
            await client.close()
        self._fail_watchers(ConnectionClosed("client closed"))

    def _request_restart(self, reason: str) -> None:
        if not self._restart.is_set():
            logger.info("%s: reconnecting cTrader session: %s", self.login, reason)
            self._restart.set()
            asyncio.create_task(self._close_client())

    async def _event_loop(self, client: CTraderJSONClient) -> None:
        while True:
            try:
                message = await client.next_event()
            except (ConnectionClosed, asyncio.CancelledError):
                return
            try:
                await self._handle_event(message)
            except Exception:  # noqa: BLE001 - an event bug must not drop the feed
                logger.exception("%s: failed to handle payload %s", self.login, message.get("payloadType"))
            self._resolve_watchers(message)

    # ---------- startup ----------

    async def _after_connect(self) -> None:
        await self._refresh_trader()
        await self._load_assets()
        await self._load_symbols()
        await self._notify_unavailable_symbols()
        # _reconcile also replays closes for positions that vanished while the
        # connector was offline, so no separate stale flush is needed.
        await self._reconcile()
        await self._protect_existing_positions()
        await self._sync_broker_history_safe(days=90)

    async def _refresh_trader(self) -> None:
        response = await self._request(
            protocol.TRADER_REQ,
            {"ctidTraderAccountId": self.ctid},
            expect=protocol.TRADER_RES,
        )
        trader = (response.get("payload") or {}).get("trader") or {}
        if trader:
            self.trader = trader
            self._trader_refreshed_at = time.monotonic()
            self.money_digits = digits_or(trader.get("moneyDigits"), 2)
            kind = account_type_name(trader.get("accountType"))
            if kind:
                self.account_type = kind

    async def _load_assets(self) -> None:
        response = await self._request(
            protocol.ASSET_LIST_REQ,
            {"ctidTraderAccountId": self.ctid},
            expect=protocol.ASSET_LIST_RES,
        )
        self.assets.clear()
        for item in (response.get("payload") or {}).get("asset") or []:
            asset_id = int_or(item.get("assetId"))
            if asset_id:
                self.assets[asset_id] = item

    async def _load_symbols(self) -> None:
        response = await self._request(
            protocol.SYMBOLS_LIST_REQ,
            {"ctidTraderAccountId": self.ctid, "includeArchivedSymbols": False},
            expect=protocol.SYMBOLS_LIST_RES,
        )
        payload = response.get("payload") or {}
        broker_symbols = list(payload.get("symbol") or [])
        if not broker_symbols:
            # Never interpret a transient empty symbol list as "this broker
            # offers nothing": that would remove every enabled root.
            logger.warning(
                "%s: empty symbol list from cTrader; keeping the existing resolution",
                self.login,
            )
            return
        self.book.load_light(broker_symbols)

        self.resolved.clear()
        self.roots_by_id.clear()
        self.wanted_symbol_ids.clear()
        unavailable: set[str] = set()
        for root in self.symbols:
            resolved = self.book.resolve(root)
            if resolved is None:
                logger.warning("%s: symbol %s is not available at this broker", self.login, root)
                unavailable.add(root)
                continue
            self.resolved[root] = resolved
            self.roots_by_id[resolved.light.symbol_id] = root
            self.book.bind_root(root, resolved.light.symbol_id)
            self.wanted_symbol_ids.add(resolved.light.symbol_id)
        self.unavailable_symbols = unavailable

        metadata_ids = [item.light.symbol_id for item in self.resolved.values()]
        await self._fetch_metadata(metadata_ids)
        self._refresh_resolved()

        await self._ensure_conversion_symbols()
        await self._subscribe_spots()

    def _refresh_resolved(self) -> None:
        for root in list(self.resolved):
            resolved = self.book.resolve(root)
            if resolved is not None:
                self.resolved[root] = resolved
                self.roots_by_id[resolved.light.symbol_id] = root

    async def _fetch_metadata(self, symbol_ids: list[int]) -> None:
        missing = [sid for sid in dict.fromkeys(symbol_ids) if sid and sid not in self.book.meta_by_id]
        for start in range(0, len(missing), 50):
            chunk = missing[start:start + 50]
            response = await self._request(
                protocol.SYMBOL_BY_ID_REQ,
                {"ctidTraderAccountId": self.ctid, "symbolId": chunk},
                expect=protocol.SYMBOL_BY_ID_RES,
            )
            self.book.load_meta(list((response.get("payload") or {}).get("symbol") or []))

    async def _ensure_conversion_symbols(self) -> None:
        deposit_asset = int_or(self.trader.get("depositAssetId"))
        if not deposit_asset:
            return
        for root, resolved in self.resolved.items():
            meta = self.book.meta_by_id.get(resolved.light.symbol_id)
            quote_asset = int_or(
                meta.quote_asset_id if meta else resolved.light.quote_asset_id
            )
            if not quote_asset or quote_asset == deposit_asset:
                continue
            try:
                response = await self._request(
                    protocol.SYMBOLS_FOR_CONVERSION_REQ,
                    {
                        "ctidTraderAccountId": self.ctid,
                        "firstAssetId": quote_asset,
                        "lastAssetId": deposit_asset,
                    },
                    expect=protocol.SYMBOLS_FOR_CONVERSION_RES,
                )
            except protocol.APIError as exc:
                logger.warning(
                    "%s: no conversion chain for %s -> deposit asset %s (%s)",
                    self.login, root, deposit_asset, exc,
                )
                continue
            for item in (response.get("payload") or {}).get("symbol") or []:
                light = self.book.add_light(item)
                if light is not None:
                    self.wanted_symbol_ids.add(light.symbol_id)

    async def _subscribe_spots(self) -> None:
        # Only subscribe to symbols this worker actually needs: traded roots
        # plus the conversion chain. Subscribing to the whole broker symbol
        # list burns the request budget and floods the session with ticks.
        symbol_ids = set(self.wanted_symbol_ids)
        pending = sorted(symbol_ids - self.subscribed)
        for start in range(0, len(pending), 50):
            chunk = pending[start:start + 50]
            try:
                await self._request(
                    protocol.SUBSCRIBE_SPOTS_REQ,
                    {
                        "ctidTraderAccountId": self.ctid,
                        "symbolId": chunk,
                        "subscribeToSpotTimestamp": True,
                    },
                    expect=protocol.SUBSCRIBE_SPOTS_RES,
                )
                self.subscribed.update(chunk)
            except protocol.APIError as exc:
                if exc.error_code == "ALREADY_SUBSCRIBED":
                    self.subscribed.update(chunk)
                    continue
                logger.warning("%s: spot subscription failed: %s", self.login, exc)

    async def _protect_existing_positions(self) -> None:
        """Repair positions that survived a connector restart without SL/TP."""
        for pid in list(self.bot_positions):
            position = self.positions.get(pid)
            if not position:
                continue
            stored = self.persisted.get(pid) or {}
            sl = float_or(stored.get("sl"))
            tp = float_or(stored.get("tp"))
            missing_sl = float_or(position.get("stopLoss")) <= 0
            missing_tp = tp > 0 and float_or(position.get("takeProfit")) <= 0
            if not missing_sl and not missing_tp:
                continue
            if sl > 0:
                if await self._amend_protection(pid, sl, tp):
                    continue
            if not missing_sl:
                # TP-only repair failed; the position still has its stop, so
                # leave it alone rather than closing a protected trade.
                logger.warning("%s: could not restore take profit on %s", self.login, pid)
                continue
            root = self._root_for_position(pid)
            logger.error("%s: bot position %s has no stop loss; closing it", self.login, pid)
            await self._event(f"ORDER_REJECTED {root} position_unprotected_{pid}")
            await self._close_position(pid, reason="EA")

    # ---------- event handling ----------

    async def _handle_event(self, message: dict) -> None:
        payload_type = int_or(message.get("payloadType"))
        payload = message.get("payload") or {}
        if payload_type == protocol.SPOT_EVENT:
            self._handle_spot(payload)
        elif payload_type == protocol.EXECUTION_EVENT:
            await self._handle_execution(payload)
        elif payload_type == protocol.TRADER_UPDATE_EVENT:
            trader = payload.get("trader") or {}
            if trader:
                self.trader = trader
                self._trader_refreshed_at = time.monotonic()
                self.money_digits = digits_or(trader.get("moneyDigits"), self.money_digits)
        elif payload_type == protocol.ORDER_ERROR_EVENT:
            logger.warning(
                "%s: cTrader order error %s: %s",
                self.login, payload.get("errorCode"), payload.get("description") or "",
            )
        elif payload_type == protocol.ACCOUNTS_TOKEN_INVALIDATED_EVENT:
            ids = [int_or(value) for value in payload.get("ctidTraderAccountIds") or []]
            if not ids or self.ctid in ids:
                self._request_restart(f"account token invalidated: {payload.get('reason') or ''}".strip())
        elif payload_type == protocol.CLIENT_DISCONNECT_EVENT:
            self._request_restart(f"server disconnect: {payload.get('reason') or ''}".strip())
        elif payload_type in (protocol.PROTO_ERROR_RES, protocol.OA_ERROR_RES):
            code = str(payload.get("errorCode") or "")
            logger.warning("%s: cTrader API error %s: %s", self.login, code, payload.get("description") or "")
            if code in ("OA_AUTH_TOKEN_EXPIRED", "CH_ACCESS_TOKEN_INVALID", "ACCOUNT_NOT_AUTHORIZED"):
                self._request_restart(code)

    def _handle_spot(self, payload: dict) -> None:
        symbol_id = int_or(payload.get("symbolId"))
        if not symbol_id:
            return
        quote = self.quotes.setdefault(symbol_id, {"bid": 0.0, "ask": 0.0, "ts": 0.0})
        bid = price(payload.get("bid"))
        ask = price(payload.get("ask"))
        if bid is not None and bid > 0:
            quote["bid"] = bid
        if ask is not None and ask > 0:
            quote["ask"] = ask
        quote["ts"] = time.monotonic()
        # cTrader's own tick time (ms) survives restarts and weekends, unlike
        # the process-local monotonic clock, so report it to the backend.
        server_ts = int_or(payload.get("timestamp"))
        quote["server_ts"] = server_ts if server_ts > 0 else int(time.time() * 1000)

    async def _handle_execution(self, payload: dict) -> None:
        execution = execution_name(payload.get("executionType"))
        position = payload.get("position")
        order = payload.get("order")
        deal = payload.get("deal")

        order_is_bot = isinstance(order, dict) and self._is_bot_trade(order)
        order_volume = int_or(((order or {}).get("tradeData") or {}).get("volume")) if order_is_bot else 0
        order_status = order_status_name((order or {}).get("orderStatus")) if isinstance(order, dict) else ""
        order_kind = order_type_name((order or {}).get("orderType")) if isinstance(order, dict) else ""
        order_id = int_or((order or {}).get("orderId")) if isinstance(order, dict) else 0
        order_pid = int_or((order or {}).get("positionId")) if isinstance(order, dict) else 0
        closing_order = bool((order or {}).get("closingOrder"))
        position_pid = int_or(position.get("positionId")) if isinstance(position, dict) else 0
        deal_pid = int_or(deal.get("positionId")) if isinstance(deal, dict) else 0
        pid = position_pid or order_pid or deal_pid

        if isinstance(position, dict) and position_pid:
            status = position_status_name(position.get("positionStatus"))
            if status == "POSITION_STATUS_OPEN":
                self.positions[position_pid] = position
                # Keep a reported-close position closed. On a netted account a
                # manual remainder can keep the same position id (and our
                # label) after the bot's volume is gone; re-adopting it would
                # report and close someone else's trade.
                if self._is_bot_trade(position) and position_pid not in self.closed_reported:
                    self.bot_positions.add(position_pid)
            elif status in ("POSITION_STATUS_CLOSED", "POSITION_STATUS_ERROR"):
                self.positions.pop(position_pid, None)

        if isinstance(order, dict):
            if (order_id
                    and order_status in ("ORDER_STATUS_ACCEPTED", "ORDER_STATUS_PARTIALLY_FILLED")
                    and order_kind in ("LIMIT", "STOP")):
                # A partially filled pending order still has a live remainder.
                self.orders[order_id] = order
                if order_is_bot:
                    self.bot_orders.add(order_id)
            elif order_id:
                self.orders.pop(order_id, None)
                self.bot_orders.discard(order_id)

            if ((order_status == "ORDER_STATUS_FILLED"
                    or execution in ("ORDER_FILLED", "ORDER_PARTIAL_FILL"))
                    and order_kind in ("LIMIT", "STOP")
                    and order_is_bot
                    and not closing_order
                    and pid
                    and pid not in self.trigger_reported):
                self.trigger_reported.add(pid)
                self.bot_positions.add(pid)
                self.closed_reported.discard(pid)
                # The position id differs from the order id on cTrader. The
                # backend needs the order id to match a copy leg to the pending
                # order it originally queued on the slave.
                order_suffix = f" order={order_id}" if order_id else ""
                await self._event(
                    f"PENDING_TRIGGERED {self._symbol_root(order)} {pid}{order_suffix}"
                )

        if (isinstance(position, dict)
                and position_pid
                and position_status_name(position.get("positionStatus")) == "POSITION_STATUS_OPEN"
                and self._is_bot_trade(position)):
            prior = int_or((self.persisted.get(position_pid) or {}).get("bot_volume"))
            fill_event = (
                execution in ("ORDER_FILLED", "ORDER_PARTIAL_FILL")
                or order_status == "ORDER_STATUS_FILLED"
            )
            deal_id = int_or((deal or {}).get("dealId"))
            cumulative = int_or((order or {}).get("executedVolume"))
            seen = self.order_filled_volume.get(order_id, 0) if order_id else 0
            filled = 0
            if (fill_event and order_is_bot and not closing_order and order_id):
                if deal_id:
                    # A partially filled order produces one deal per execution
                    # event; deal ids are unique, order ids repeat.
                    if deal_id not in self.seen_fill_deals:
                        self.seen_fill_deals.add(deal_id)
                        filled = int_or((deal or {}).get("filledVolume"))
                        if filled <= 0 and cumulative > seen:
                            filled = cumulative - seen
                        if filled <= 0:
                            filled = order_volume
                        if cumulative > 0:
                            self.order_filled_volume[order_id] = max(seen, cumulative)
                elif cumulative > 0:
                    # ``executedVolume`` is cumulative for the order, so partial
                    # fills add only the delta. A duplicated event adds nothing.
                    if cumulative > seen:
                        filled = cumulative - seen
                        self.order_filled_volume[order_id] = cumulative
                elif order_id not in self.opened_orders:
                    self.opened_orders.add(order_id)
                    filled = order_volume
                if len(self.seen_fill_deals) > 5000:
                    self.seen_fill_deals = set(list(self.seen_fill_deals)[-2500:])
                if len(self.opened_orders) > 2000:
                    self.opened_orders = set(list(self.opened_orders)[-1000:])
            if filled > 0:
                position_volume = int_or((position.get("tradeData") or {}).get("volume"))
                bot_total = (prior + filled) if prior > 0 else filled
                if position_volume > 0:
                    # The bot can never own more than the position that exists.
                    bot_total = min(bot_total, position_volume)
                self._remember_position(
                    position_pid, position, bot_volume=bot_total,
                    entry=float_or((deal or {}).get("executionPrice")),
                )
            else:
                # Position refresh / amendment: keep the volume we already own.
                self._remember_position(position_pid, position)

        if isinstance(deal, dict):
            detail = deal.get("closePositionDetail")
            if isinstance(detail, dict) and deal_pid:
                label = str(deal.get("label") or "")
                known = (deal_pid not in self.closed_reported
                         and (deal_pid in self.bot_positions
                              or deal_pid in self.persisted
                              or label.startswith(LABEL_PREFIX)))
                if known:
                    digits = digits_or(
                        detail.get("moneyDigits"),
                        digits_or(deal.get("moneyDigits"), self.money_digits),
                    )
                    entry = self.partial_closes.setdefault(deal_pid, {
                        "profit": 0.0, "volume": 0, "last_price": 0.0, "reason": "MANUAL",
                    })
                    entry["profit"] += net_close_profit(detail, digits)
                    entry["volume"] += int_or(detail.get("closedVolume"), int_or(deal.get("filledVolume")))
                    entry["last_price"] = float_or(deal.get("executionPrice"))
                    entry["reason"] = classify_close(
                        deal, self.positions.get(deal_pid), digits,
                        initiated_by_bot=deal_pid in self.closing_intents,
                    )
                    open_position = self.positions.get(deal_pid)
                    if open_position:
                        open_volume = int_or((open_position.get("tradeData") or {}).get("volume"))
                        bot_volume = int_or(
                            (self.persisted.get(deal_pid) or {}).get("bot_volume"), open_volume
                        )
                        # On a netted account the position can keep living after
                        # our own volume is gone. Report our close as soon as the
                        # accumulated closing volume covers the bot volume.
                        target = min(open_volume, bot_volume) if open_volume else bot_volume
                        if target and entry["volume"] >= target:
                            self.positions.pop(deal_pid, None)
                            await self._flush_closed(deal_pid)

        # Balance changes on fills and closes. Force the next report to re-read
        # the trader so balance/equity/margin/free margin never stay stale after
        # a manual close.
        if (
            execution in ("ORDER_FILLED", "ORDER_PARTIAL_FILL")
            or order_status == "ORDER_STATUS_FILLED"
            or (
                isinstance(position, dict)
                and position_status_name(position.get("positionStatus"))
                in ("POSITION_STATUS_CLOSED", "POSITION_STATUS_ERROR")
            )
        ):
            self._trader_refreshed_at = 0.0

    def _is_bot_trade(self, entity: dict) -> bool:
        trade = entity.get("tradeData") or {}
        if str(trade.get("label") or "").startswith(LABEL_PREFIX):
            return True
        return False

    def _resolve_watchers(self, message: dict) -> None:
        for watcher in list(self._watchers):
            if watcher.future.done():
                self._watchers.remove(watcher)
                continue
            try:
                matched = watcher.predicate(message)
            except Exception:  # noqa: BLE001 - a predicate bug should not break the feed
                logger.exception("%s: watcher predicate failed", self.login)
                matched = False
            if matched:
                self._watchers.remove(watcher)
                watcher.future.set_result(message)

    def _fail_watchers(self, exc: Exception) -> None:
        for watcher in list(self._watchers):
            if not watcher.future.done():
                watcher.future.set_exception(exc)
        self._watchers.clear()

    # ---------- main loop ----------

    async def _main_loop(self) -> None:
        next_commands = 0.0
        next_report = 0.0
        next_reconcile = 0.0
        next_protect = time.monotonic() + PROTECTION_SWEEP_SECONDS
        next_history = time.monotonic() + HISTORY_SYNC_SECONDS
        while not self._stop.is_set() and not self._restart.is_set():
            client = self._client
            if client is None or client.closed:
                return
            now = time.monotonic()
            if now >= next_commands:
                await self._poll_commands()
                next_commands = time.monotonic() + self.settings.command_interval
            if self._restart.is_set() or self._stop.is_set():
                return
            now = time.monotonic()
            if now >= next_reconcile:
                await self._reconcile()
                next_reconcile = time.monotonic() + self.settings.reconcile_interval
            now = time.monotonic()
            if now >= next_protect:
                # Safety net for a protection amend that failed and whose
                # immediate close also failed: retry, then close, instead of
                # leaving a bot position unprotected until the next restart.
                await self._protect_existing_positions()
                next_protect = time.monotonic() + PROTECTION_SWEEP_SECONDS
            now = time.monotonic()
            if now >= next_history:
                await self._sync_broker_history_safe(days=2)
                next_history = time.monotonic() + HISTORY_SYNC_SECONDS
            now = time.monotonic()
            if now >= next_report:
                await self._report()
                next_report = time.monotonic() + self.settings.report_interval
            await self._sleep_or_stop(0.2)

    async def _poll_commands(self) -> None:
        try:
            lines = await self.backend.get_commands(self.login)
        except BackendError as exc:
            logger.warning("%s: command poll failed: %s", self.login, exc)
            self._note_backend_failure(exc)
            return
        for line in lines:
            try:
                await self._execute_command(line)
            except asyncio.CancelledError:
                raise
            except (protocol.APIError, OrderFailed, SizingError, CommandError) as exc:
                logger.warning("%s: command %r failed: %s", self.login, line, exc)
            except Exception:  # noqa: BLE001 - one bad command must not kill the worker
                logger.exception("%s: command %r crashed", self.login, line)

    async def _execute_command(self, line: str) -> None:
        parsed = parse_command(line)
        if parsed is None:
            logger.warning("%s: ignoring unknown command %r", self.login, line)
            return
        name, args = parsed
        if name == "SYMBOLS":
            await self._apply_symbols(args[0])
        elif name == "RISK":
            self.risk_pct = float_or(args[0], self.risk_pct)
        elif name == "MAXPOS":
            self.max_positions = int_or(args[0], self.max_positions)
        elif name == "RRR":
            self.rrr = float_or(args[0], self.rrr)
        elif name == "BLOCK":
            self.blocked = True
            await self._close_all_bot()
        elif name == "UNBLOCK":
            self.blocked = False
        elif name == "CLOSE":
            await self._command_close(int_or(args[0]))
        elif name == "CLOSE_ALL":
            await self._close_all_bot()
        elif name == "DELETE_PENDING":
            await self._command_cancel(int_or(args[0]))
        elif name == "OPEN":
            await self._command_open(args)
        elif name == "OPEN_MARKET":
            await self._command_open_market(args)
        elif name == "CANDLES":
            await self._command_candles(args)

    async def _apply_symbols(self, raw: str) -> None:
        incoming: list[str] = []
        seen: set[str] = set()
        for item in (raw or "").split(","):
            item = item.strip()
            key = item.upper()
            if not item or key in seen:
                continue
            seen.add(key)
            incoming.append(item)
        if [item.upper() for item in incoming] == [item.upper() for item in self.symbols]:
            return
        self.symbols = incoming[:20]
        self._symbols_dirty = True
        await self._load_symbols()
        self._symbols_dirty = False
        await self._notify_unavailable_symbols()

    async def _notify_unavailable_symbols(self) -> None:
        """Tell the backend which enabled roots this broker does not offer.

        The backend removes them from the account, so the PWA stops offering a
        symbol that can never be executed here and the operator gets a clear
        notification instead of a silent order failure.
        """
        for root in sorted(self.unavailable_symbols):
            await self._event(f"SYMBOL_UNAVAILABLE {root}")

    # ---------- commands ----------

    async def _command_close(self, position_id: int) -> None:
        if not position_id:
            raise CommandError("missing position id")
        position = self.positions.get(position_id)
        if position is None:
            await self._reconcile()
            position = self.positions.get(position_id)
        if position is None:
            logger.info("%s: close ignored, position %s is already gone", self.login, position_id)
            return
        root = self._root_for_position(position_id)
        if not await self._close_position(position_id, reason="EA"):
            await self._event(f"ORDER_REJECTED {root} close_failed")

    async def _command_cancel(self, order_id: int) -> None:
        if not order_id:
            raise CommandError("missing order id")
        order = self.orders.get(order_id)
        if order is None:
            await self._reconcile()
            order = self.orders.get(order_id)
        if order is None:
            return
        if not self._is_bot_trade(order):
            logger.warning("%s: refusing to cancel manual order %s", self.login, order_id)
            return
        if not await self._cancel_order(order_id):
            await self._event(f"ORDER_REJECTED {self._symbol_root(order)} cancel_failed")

    async def _command_open(self, args: list[str]) -> None:
        side = side_name(args[0])
        root = args[1].strip()
        entry = float_or(args[2])
        sl = float_or(args[3])
        tp = float_or(args[4])
        risk_pct = float_or(args[5], self.risk_pct)
        if side not in ("BUY", "SELL"):
            raise CommandError(f"invalid side {args[0]!r}")
        if self.blocked:
            await self._event(f"ORDER_REJECTED {root} trading_blocked")
            return
        try:
            resolved = await self._require_symbol(root)
        except SizingError:
            await self._event(f"ORDER_REJECTED {root} symbol_unavailable")
            return
        symbol_id = resolved.light.symbol_id
        digits = self._digits(symbol_id)
        # The backend builds prices with up to 10 decimals. cTrader rejects any
        # price with more decimals than the symbol allows (BTCUSD is 2, not 5),
        # so normalise before sizing or sending.
        entry = round_price(entry, digits)
        sl = round_price(sl, digits)
        tp = round_price(tp, digits)
        quote = self._live_quote(symbol_id, QUOTE_ORDER_MAX_AGE)
        if quote is None:
            await self._event(f"ORDER_REJECTED {root} market_closed_or_no_quotes")
            return
        bid, ask = quote
        tolerance = self.settings.price_tolerance_points * (10 ** -digits)
        kind = order_plan(side, entry, ask, bid, tolerance)
        if not kind:
            await self._event(f"ORDER_REJECTED {root} market_closed_or_no_quotes")
            return
        effective_entry = entry
        if kind == "MARKET":
            effective_entry = round_price(ask if side == "BUY" else bid, digits)
        if not valid_protection(side, effective_entry, sl, tp):
            await self._event(f"ORDER_REJECTED {root} invalid_sl_or_tp")
            return
        if self._netted_conflict(resolved.light.symbol_id, side):
            await self._event(f"ORDER_REJECTED {root} netted_opposite_exposure")
            return
        if kind == "MARKET":
            await self._send_market(resolved, side, effective_entry, sl, tp, risk_pct)
        else:
            await self._send_pending(resolved, side, kind, entry, sl, tp, risk_pct)

    async def _command_open_market(self, args: list[str]) -> None:
        side = side_name(args[0])
        root = args[1].strip()
        sl = float_or(args[2])
        risk_pct = float_or(args[3], self.risk_pct)
        rrr = float_or(args[4], self.rrr)
        if side not in ("BUY", "SELL"):
            raise CommandError(f"invalid side {args[0]!r}")
        if self.blocked:
            await self._event(f"ORDER_REJECTED {root} trading_blocked")
            return
        try:
            resolved = await self._require_symbol(root)
        except SizingError:
            await self._event(f"ORDER_REJECTED {root} symbol_unavailable")
            return
        symbol_id = resolved.light.symbol_id
        digits = self._digits(symbol_id)
        sl = round_price(sl, digits)
        quote = self._live_quote(symbol_id, QUOTE_ORDER_MAX_AGE)
        if quote is None:
            await self._event(f"ORDER_REJECTED {root} market_closed_or_no_quotes")
            return
        bid, ask = quote
        entry = round_price(ask if side == "BUY" else bid, digits)
        tp = round_price(compute_tp(side, entry, sl, max(0.1, rrr)), digits)
        if not valid_protection(side, entry, sl, tp):
            await self._event(f"ORDER_REJECTED {root} invalid_sl")
            return
        if self._netted_conflict(symbol_id, side):
            await self._event(f"ORDER_REJECTED {root} netted_opposite_exposure")
            return
        await self._send_market(resolved, side, entry, sl, tp, risk_pct)

    # ---------- candles ----------

    async def _command_candles(self, args: list[str]) -> None:
        root = (args[0] or "").strip()
        tf_name = (args[1] or "").strip().upper()
        count = int_or(args[2], 300)
        if not root or tf_name not in TRENDBAR_TIMEFRAMES:
            return
        resolved = await self._require_symbol(root)
        if resolved is None:
            return
        try:
            bars = await self._fetch_trendbars(
                resolved.light.symbol_id,
                TRENDBAR_TIMEFRAMES[tf_name],
                min(count, 500),
            )
        except (protocol.APIError, ConnectionClosed) as exc:
            logger.warning("%s: candles for %s failed: %s", self.login, root, exc)
            return
        if not bars:
            return
        payload = ";".join(
            f"{t},{o:.5f},{h:.5f},{l:.5f},{c:.5f},{v}" for t, o, h, l, c, v in bars
        )
        try:
            await self.backend.post_candles(self.login, root, tf_name, payload)
        except BackendError as exc:
            self._note_backend_failure(exc)
            logger.warning("%s: candle report failed: %s", self.login, exc)

    async def _fetch_trendbars(self, symbol_id: int, period: int, count: int) -> list:
        now_ms = int(time.time() * 1000)
        # count x period span; period enums are minute-based except weeks/months.
        span_ms = _TRENDBAR_SPAN_MS.get(period, 60_000) * count
        payload = {
            "ctidTraderAccountId": int(self.ctid),
            "symbolId": int(symbol_id),
            "period": int(period),
            "fromTimestamp": now_ms - span_ms,
            "toTimestamp": now_ms,
            "count": int(count),
        }
        res = await self._request(protocol.GET_TRENDBARS_REQ, payload,
                                  expect=protocol.GET_TRENDBARS_RES)
        digits = self._digits(symbol_id)
        bars = []
        for tb in res.get("trendbar") or []:
            low = int_or(tb.get("low"))
            if low <= 0:
                continue
            low_p = low / 100000.0
            o = (low + int_or(tb.get("deltaOpen"))) / 100000.0
            h = (low + int_or(tb.get("deltaHigh"))) / 100000.0
            c = (low + int_or(tb.get("deltaClose"))) / 100000.0
            t_min = int_or(tb.get("utcTimestampInMinutes"))
            bars.append((
                t_min * 60,
                round(o, digits), round(h, digits), round(low_p, digits),
                round(c, digits), int_or(tb.get("volume")),
            ))
        bars.sort(key=lambda b: b[0])
        return bars

    # ---------- symbol / quote helpers ----------

    async def _require_symbol(self, root: str):
        resolved = self._resolve_root(root)
        if resolved is None and self._symbols_dirty:
            await self._load_symbols()
            self._symbols_dirty = False
            resolved = self._resolve_root(root)
        if resolved is None:
            raise SizingError("symbol_unavailable")
        return resolved

    def _resolve_root(self, root: str):
        key = (root or "").upper()
        for configured, resolved in self.resolved.items():
            if configured.upper() == key:
                return resolved
        return None

    def _live_quote(self, symbol_id: int, max_age: float) -> tuple[float, float] | None:
        quote = self.quotes.get(symbol_id)
        if not quote:
            return None
        if self._quote_age(quote) > max_age:
            return None
        bid = float_or(quote.get("bid"))
        ask = float_or(quote.get("ask"))
        if bid <= 0 or ask <= 0:
            return None
        return bid, ask

    @staticmethod
    def _quote_age(quote: dict) -> float:
        """Seconds since the tick, preferring cTrader's server timestamp.

        A reconnect during a weekend returns one snapshot from Friday's close;
        its arrival time is fresh, so arrival alone would make a dead market
        look tradable for a minute.
        """
        server_ts = int_or(quote.get("server_ts"))
        if server_ts > 0:
            return max(0.0, time.time() - server_ts / 1000.0)
        return time.monotonic() - float_or(quote.get("ts"))

    def _digits(self, symbol_id: int) -> int:
        meta = self.book.meta_by_id.get(symbol_id)
        return int(meta.digits) if meta and meta.digits > 0 else 5

    def _lot_size(self, symbol_id: int) -> int:
        meta = self.book.meta_by_id.get(symbol_id)
        return int(meta.lot_size) if meta and meta.lot_size > 0 else DEFAULT_LOT_SIZE

    def _symbol_root(self, entity: dict) -> str:
        trade = entity.get("tradeData") or {}
        symbol_id = int_or(trade.get("symbolId"))
        # Prefer the root captured when the position was first seen. A later
        # symbol-list edit clears roots_by_id, and the broker name (e.g.
        # ``US100.cash``) must not replace the Discord root (``US100``).
        pid = int_or(entity.get("positionId"))
        stored = self.persisted.get(pid) or {}
        if stored and int_or(stored.get("symbol_id")) == symbol_id:
            root = str(stored.get("root") or "")
            if root:
                return root
        if symbol_id in self.roots_by_id:
            return self.roots_by_id[symbol_id]
        name = self.book.name_for_id(symbol_id)
        return (name or f"symbol{symbol_id}").replace("/", "")

    def _root_for_position(self, position_id: int) -> str:
        position = self.positions.get(position_id)
        if position:
            return self._symbol_root(position)
        stored = self.persisted.get(position_id) or {}
        return str(stored.get("root") or f"position{position_id}")

    def _netted_conflict(self, symbol_id: int, side: str) -> bool:
        """True if an opposite-side exposure exists on a netting account.

        On a NETTED account an opposite order would reduce or flip someone
        else's position and destroy the bot-volume accounting. Same-side adds
        are safe; opposite-side entries are refused with an alert.
        """
        if self.account_type not in ("NETTED", "SPREAD_BETTING"):
            return False
        opposite = "SELL" if side == "BUY" else "BUY"
        for entity in list(self.positions.values()) + list(self.orders.values()):
            trade = entity.get("tradeData") or {}
            if int_or(trade.get("symbolId")) != symbol_id:
                continue
            if side_name(trade.get("tradeSide")) == opposite:
                return True
        return False

    # ---------- sizing ----------

    def _balance(self) -> float:
        return money(self.trader.get("balance"), self.money_digits)

    def _equity(self) -> float:
        return self._balance() + sum(self.pnl.values())

    def _margin_used(self) -> float:
        total = 0.0
        for position in self.positions.values():
            digits = digits_or(position.get("moneyDigits"), self.money_digits)
            total += money(position.get("usedMargin"), digits)
        return total

    async def _margin_per_lot(self, symbol_id: int, side: str) -> float | None:
        lot_size = self._lot_size(symbol_id)
        if lot_size <= 0:
            return None
        try:
            response = await self._request(
                protocol.EXPECTED_MARGIN_REQ,
                {
                    "ctidTraderAccountId": self.ctid,
                    "symbolId": symbol_id,
                    "volume": [lot_size],
                },
                expect=protocol.EXPECTED_MARGIN_RES,
            )
        except protocol.APIError as exc:
            logger.warning("%s: expected-margin request failed: %s", self.login, exc)
            return None
        payload = response.get("payload") or {}
        digits = digits_or(payload.get("moneyDigits"), self.money_digits)
        rows = payload.get("margin") or []
        chosen = None
        for row in rows:
            if int_or(row.get("volume")) == lot_size:
                chosen = row
                break
        if chosen is None and rows:
            chosen = rows[0]
        if not chosen:
            return None
        value = chosen.get("buyMargin") if side == "BUY" else chosen.get("sellMargin")
        result = money(value, digits)
        return result if result > 0 else None

    async def _size(self, resolved, side: str, entry: float, sl: float, risk_pct: float):
        symbol_id = resolved.light.symbol_id
        meta = self.book.meta_by_id.get(symbol_id)
        if meta is None or meta.lot_size <= 0:
            raise SizingError("symbol_metadata_incomplete")
        if not meta.tradable:
            raise SizingError("symbol_not_tradable")
        deposit_asset = int_or(self.trader.get("depositAssetId"))
        rate = conversion_rate(
            meta.quote_asset_id,
            deposit_asset,
            self.book.edges(),
            self.quotes,
        )
        if rate is None or rate <= 0:
            raise SizingError("conversion_unavailable")
        margin_per_lot = await self._margin_per_lot(symbol_id, side)
        return size_order(
            balance=self._balance(),
            risk_pct=risk_pct,
            entry=entry,
            stop_loss=sl,
            lot_size=meta.lot_size,
            min_volume=meta.min_volume,
            step_volume=meta.step_volume,
            max_volume=meta.max_volume,
            quote_to_deposit=rate,
            margin_per_lot=margin_per_lot,
            equity=self._equity(),
            margin_used=self._margin_used(),
            margin_cap_pct=self.settings.margin_cap_pct,
        )

    # ---------- order placement ----------

    def _new_client_order_id(self) -> str:
        return f"gr-{uuid.uuid4().hex[:24]}"

    def _watch(self, predicate: Callable[[dict], bool]) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._watchers.append(_Watcher(predicate=predicate, future=future))
        return future

    def _execution_predicate(self, client_order_id: str, symbol_id: int,
                             side: str, accepted_kinds: tuple[str, ...]):
        def predicate(message: dict) -> bool:
            if int_or(message.get("payloadType")) != protocol.EXECUTION_EVENT:
                return False
            payload = message.get("payload") or {}
            execution = execution_name(payload.get("executionType"))
            if execution in ("ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_PARTIAL_FILL"):
                if execution not in accepted_kinds:
                    return False
            elif execution not in ("ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_EXPIRED"):
                return False
            order = payload.get("order") or {}
            trade = order.get("tradeData") or {}
            position = payload.get("position") or {}
            position_trade = position.get("tradeData") or {}
            echoed = str(order.get("clientOrderId") or "")
            if echoed:
                return echoed == client_order_id
            # Some brokers do not echo clientOrderId on every event. Fall back
            # to our label + symbol + side, which is safe because only one
            # order is in flight at a time on this worker.
            label = str(trade.get("label") or position_trade.get("label") or "")
            sym = int_or(trade.get("symbolId"), int_or(position_trade.get("symbolId")))
            entity_side = side_name(trade.get("tradeSide")) or side_name(position_trade.get("tradeSide"))
            return label.startswith(LABEL_PREFIX) and sym == symbol_id and entity_side == side
        return predicate

    async def _await(self, future: asyncio.Future, timeout: float, what: str) -> dict:
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise OrderFailed("order_timeout", what) from None

    async def _await_execution(self, future: asyncio.Future, timeout: float, what: str) -> dict:
        """Wait for an execution event, but stop early on an order error event."""
        error_future = self._watch(
            lambda message: int_or(message.get("payloadType")) == protocol.ORDER_ERROR_EVENT
        )
        try:
            done, _ = await asyncio.wait(
                {future, error_future}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise OrderFailed("order_timeout", what)
            if future in done:
                return future.result()
            payload = error_future.result().get("payload") or {}
            raise OrderFailed(
                str(payload.get("errorCode") or "ORDER_ERROR"),
                str(payload.get("description") or ""),
            )
        finally:
            self._discard_watcher(error_future)

    def _rejection(self, event: dict) -> OrderFailed | None:
        payload = event.get("payload") or {}
        execution = execution_name(payload.get("executionType"))
        if execution not in ("ORDER_REJECTED", "ORDER_CANCELLED", "ORDER_EXPIRED"):
            return None
        return OrderFailed(
            str(payload.get("errorCode") or execution.lower()),
            str(payload.get("description") or ""),
        )

    async def _send_market(self, resolved, side: str, entry: float, sl: float,
                           tp: float, risk_pct: float) -> None:
        symbol_id = resolved.light.symbol_id
        root = self._resolve_root_name(resolved)
        digits = self._digits(symbol_id)
        entry = round_price(entry, digits)
        sl = round_price(sl, digits)
        tp = round_price(tp, digits)
        if not valid_protection(side, entry, sl, tp):
            await self._event(f"ORDER_REJECTED {root} invalid_sl_or_tp")
            return
        try:
            sizing = await self._size(resolved, side, entry, sl, risk_pct)
        except SizingError as exc:
            await self._report_sizing_error(root, exc)
            return
        if sizing.margin_capped:
            await self._event(
                f"LOT_CAPPED {root} requested_lots={sizing.ideal_lots:.2f} "
                f"actual_lots={sizing.lots:.2f} requested_risk_pct={risk_pct:.2f} "
                f"actual_risk_pct={sizing.actual_risk_pct:.2f} "
                f"margin_allows_lots={sizing.margin_allows_lots:.2f}"
            )

        client_order_id = self._new_client_order_id()
        # Wait for the fill itself. Do not resolve on ORDER_ACCEPTED: the fill
        # event can already be queued behind it, and re-registering a watcher
        # afterwards would miss that event and time out on a live position.
        future = self._watch(
            self._execution_predicate(
                client_order_id, symbol_id, side, ("ORDER_FILLED", "ORDER_PARTIAL_FILL")
            )
        )
        payload = {
            "ctidTraderAccountId": self.ctid,
            "symbolId": symbol_id,
            "orderType": "MARKET",
            "tradeSide": side,
            "volume": sizing.volume,
            "label": LABEL,
            "clientOrderId": client_order_id,
            "comment": f"GuardRails {risk_pct:g}%",
        }
        sent_ms = int(time.time() * 1000)
        try:
            await self._client.send(protocol.NEW_ORDER_REQ, payload)
        except Exception:
            self._discard_watcher(future)
            raise

        try:
            event = await self._await_execution(future, ORDER_FILL_TIMEOUT, f"market {root}")
        except OrderFailed as exc:
            self._discard_watcher(future)
            if exc.code != "order_timeout":
                await self._event(f"ORDER_REJECTED {root} send_failed_{exc.code}")
                return
            # The fill event may have been lost while the order still reached
            # the broker. Adopt a matching bot position instead of reporting a
            # rejection that would leave it unprotected and untracked.
            try:
                await self._reconcile()
            except Exception:  # noqa: BLE001 - fall through to rejection
                pass
            pid = self._find_recent_position(symbol_id, side, sent_ms)
            if not pid:
                await self._event(f"ORDER_REJECTED {root} order_timeout")
                return
            event = {
                "payload": {
                    "executionType": "ORDER_FILLED",
                    "position": self.positions.get(pid) or {},
                }
            }
        finally:
            self._discard_watcher(future)

        rejection = self._rejection(event)
        if rejection:
            await self._event(f"ORDER_REJECTED {root} send_failed_{rejection.code}")
            return

        payload = event.get("payload") or {}
        position = payload.get("position") or {}
        pid = int_or(position.get("positionId"))
        if not pid:
            pid = int_or((payload.get("order") or {}).get("positionId"))
        if not pid:
            pid = int_or((payload.get("deal") or {}).get("positionId"))
        if not pid:
            await self._reconcile()
            pid = self._find_recent_position(symbol_id, side, sent_ms)
        if not pid:
            await self._event(f"ORDER_REJECTED {root} order_filled_no_position")
            return

        fill_price = float_or(position.get("price"), entry)
        if not position:
            await self._reconcile()
            position = self.positions.get(pid) or {}
        if not position:
            position = {
                "positionId": pid,
                "price": fill_price,
                "tradeData": {
                    "symbolId": symbol_id,
                    "tradeSide": side,
                    "volume": sizing.volume,
                    "label": LABEL,
                    "openTimestamp": int(time.time() * 1000),
                },
            }
            self.positions[pid] = position
        stored = self.persisted.get(pid) or {}
        bot_volume = int_or(stored.get("bot_volume"))
        if bot_volume <= 0:
            bot_volume = sizing.volume
        volume = int_or((position.get("tradeData") or {}).get("volume"), bot_volume)
        volume = min(volume, bot_volume) if volume > 0 else bot_volume
        lots = lots_from_volume(volume, self._lot_size(symbol_id))
        self.bot_positions.add(pid)
        self.closed_reported.discard(pid)
        self._remember_position(pid, position, bot_volume=bot_volume, entry=fill_price)
        if not await self._amend_protection(pid, sl, tp):
            logger.error("%s: protection amend failed for %s; closing", self.login, pid)
            closed = await self._close_position(pid, reason="EA")
            suffix = "protection_failed" if closed else "protection_failed_unclosed"
            await self._event(f"ORDER_REJECTED {root} {suffix}")
            return
        await self._event(
            f"POSITION_OPENED {root} {pid} {side} {lots:.2f} {fmt_price(fill_price, self._digits(symbol_id))}"
        )

    async def _send_pending(self, resolved, side: str, kind: str, entry: float,
                            sl: float, tp: float, risk_pct: float) -> None:
        symbol_id = resolved.light.symbol_id
        root = self._resolve_root_name(resolved)
        digits = self._digits(symbol_id)
        entry = round_price(entry, digits)
        sl = round_price(sl, digits)
        tp = round_price(tp, digits)
        if not valid_protection(side, entry, sl, tp):
            await self._event(f"ORDER_REJECTED {root} invalid_sl_or_tp")
            return
        try:
            sizing = await self._size(resolved, side, entry, sl, risk_pct)
        except SizingError as exc:
            await self._report_sizing_error(root, exc)
            return
        if sizing.margin_capped:
            await self._event(
                f"LOT_CAPPED {root} requested_lots={sizing.ideal_lots:.2f} "
                f"actual_lots={sizing.lots:.2f} requested_risk_pct={risk_pct:.2f} "
                f"actual_risk_pct={sizing.actual_risk_pct:.2f} "
                f"margin_allows_lots={sizing.margin_allows_lots:.2f}"
            )

        client_order_id = self._new_client_order_id()
        # An entry that is already through the market can fill before the
        # broker sends ORDER_ACCEPTED. Treat any terminal fill as success so a
        # live position is never reported as a timed-out/rejected order.
        future = self._watch(
            self._execution_predicate(
                client_order_id, symbol_id, side,
                ("ORDER_ACCEPTED", "ORDER_FILLED", "ORDER_PARTIAL_FILL"),
            )
        )
        payload = {
            "ctidTraderAccountId": self.ctid,
            "symbolId": symbol_id,
            "orderType": kind,
            "tradeSide": side,
            "volume": sizing.volume,
            "timeInForce": "GOOD_TILL_CANCEL",
            "stopLoss": sl,
            "takeProfit": tp,
            "label": LABEL,
            "clientOrderId": client_order_id,
            "comment": f"GuardRails {risk_pct:g}%",
        }
        if kind == "LIMIT":
            payload["limitPrice"] = entry
        else:
            payload["stopPrice"] = entry
        try:
            await self._client.send(protocol.NEW_ORDER_REQ, payload)
            event = await self._await_execution(future, ORDER_FILL_TIMEOUT, f"pending {root}")
        except OrderFailed as exc:
            await self._event(f"ORDER_REJECTED {root} send_failed_{exc.code}")
            return
        finally:
            self._discard_watcher(future)
        rejection = self._rejection(event)
        if rejection:
            await self._event(f"ORDER_REJECTED {root} send_failed_{rejection.code}")
            return
        payload = event.get("payload") or {}
        execution = execution_name(payload.get("executionType"))
        order_id = int_or((payload.get("order") or {}).get("orderId"))
        if execution in ("ORDER_FILLED", "ORDER_PARTIAL_FILL"):
            pid = int_or((payload.get("position") or {}).get("positionId"))
            if not pid:
                pid = int_or((payload.get("order") or {}).get("positionId"))
            if pid and pid not in self.trigger_reported:
                # _handle_execution normally announces this, but the broker
                # does not always report ORDER_STATUS_FILLED on a fill event.
                self.trigger_reported.add(pid)
                self.bot_positions.add(pid)
                self.closed_reported.discard(pid)
                order_suffix = f" order={order_id}" if order_id else ""
                await self._event(f"PENDING_TRIGGERED {root} {pid}{order_suffix}")
            if pid:
                position = payload.get("position") or self.positions.get(pid) or {}
                if (float_or(position.get("stopLoss")) <= 0
                        or float_or(position.get("takeProfit")) <= 0):
                    if not await self._amend_protection(pid, sl, tp):
                        logger.error("%s: pending protection missing for %s; closing", self.login, pid)
                        closed = await self._close_position(pid, reason="EA")
                        suffix = "protection_failed" if closed else "protection_failed_unclosed"
                        await self._event(f"ORDER_REJECTED {root} {suffix}")
                        return
        await self._reconcile()
        if order_id and order_id in self.orders:
            # Mirror MT5's PENDING_PLACED so a copy slave can map the broker's
            # order id to the leg that queued it. cTrader uses a different id
            # for the position created when this order later fills.
            order = self.orders[order_id]
            order_digits = self._digits(symbol_id)
            await self._event(
                f"PENDING_PLACED type={pending_type_label(order)} symbol={root} "
                f"ticket={order_id} entry={fmt_price(pending_price(order), order_digits)} "
                f"sl={fmt_price(order.get('stopLoss'), order_digits)} "
                f"tp={fmt_price(order.get('takeProfit'), order_digits)} "
                f"lots={sizing.lots:.2f} risk={risk_pct:g}"
            )
        elif order_id:
            # The order filled between ORDER_ACCEPTED and this snapshot (the
            # fill event can be processed before this coroutine resumes), so
            # verify/repair protection now rather than waiting for the sweep.
            await self._protect_existing_positions()

    def _find_recent_position(self, symbol_id: int, side: str, since_ms: int = 0) -> int:
        newest = 0
        newest_ts = 0
        for pid, position in self.positions.items():
            trade = position.get("tradeData") or {}
            if int_or(trade.get("symbolId")) != symbol_id:
                continue
            if side_name(trade.get("tradeSide")) != side:
                continue
            if not self._is_bot_trade(position):
                continue
            opened = int_or(trade.get("openTimestamp"))
            # Allow for a small clock difference between this host and the
            # broker; the caller only wants "a position opened around send time".
            if since_ms and opened and opened < since_ms - 120_000:
                continue
            if opened >= newest_ts:
                newest_ts = opened
                newest = pid
        return newest

    def _discard_watcher(self, future: asyncio.Future) -> None:
        for watcher in list(self._watchers):
            if watcher.future is future:
                self._watchers.remove(watcher)
        if future.done() and not future.cancelled():
            try:
                future.exception()
            except Exception:
                pass

    async def _report_sizing_error(self, root: str, exc: SizingError) -> None:
        if exc.reason == "margin_cap_below_min_lot":
            await self._event(
                f"ORDER_REJECTED {root} margin_cap_below_min_lot "
                f"requested_lots={float_or(exc.details.get('requested_lots')):.2f} "
                f"margin_allows_lots={float_or(exc.details.get('margin_allows_lots')):.2f} "
                f"broker_min_lot={float_or(exc.details.get('broker_min_lot')):.2f}"
            )
            return
        await self._event(f"ORDER_REJECTED {root} {exc.reason}")

    def _resolve_root_name(self, resolved) -> str:
        for root, item in self.resolved.items():
            if item.light.symbol_id == resolved.light.symbol_id:
                return root
        return resolved.light.name.replace("/", "")

    async def _amend_protection(self, position_id: int, sl: float, tp: float) -> bool:
        symbol_id = int_or(((self.positions.get(position_id) or {}).get("tradeData") or {}).get("symbolId"))
        meta = self.book.meta_by_id.get(symbol_id)
        digits = self._digits(symbol_id)
        sl = round_price(sl, digits)
        tp = round_price(tp, digits)
        guaranteed = bool(self.trader.get("isLimitedRisk")) and bool(meta and meta.guaranteed_stop_loss)
        tolerance = max(1e-8, 5 * (10 ** -digits))

        def predicate(message: dict) -> bool:
            if int_or(message.get("payloadType")) != protocol.EXECUTION_EVENT:
                return False
            payload = message.get("payload") or {}
            execution = execution_name(payload.get("executionType"))
            position = payload.get("position") or {}
            order = payload.get("order") or {}
            pid = int_or(position.get("positionId"), int_or(order.get("positionId")))
            if pid != position_id:
                return False
            if execution in ("ORDER_REJECTED", "ORDER_CANCEL_REJECTED"):
                return True
            if execution not in ("ORDER_REPLACED", "ORDER_ACCEPTED", "ORDER_FILLED"):
                return False
            got_sl = float_or(position.get("stopLoss"), float_or(order.get("stopLoss")))
            got_tp = float_or(position.get("takeProfit"), float_or(order.get("takeProfit")))
            sl_ok = sl <= 0 or abs(got_sl - sl) <= tolerance
            tp_ok = tp <= 0 or abs(got_tp - tp) <= tolerance
            return sl_ok and tp_ok

        future = self._watch(predicate)
        payload = {
            "ctidTraderAccountId": self.ctid,
            "positionId": position_id,
        }
        # cTrader rejects an amend that carries a zero level; omit an absent
        # side instead of turning TP-only repairs into failures.
        if sl > 0:
            payload["stopLoss"] = sl
        if tp > 0:
            payload["takeProfit"] = tp
        if len(payload) == 2:
            self._discard_watcher(future)
            return True
        if guaranteed:
            payload["guaranteedStopLoss"] = True
        try:
            await self._client.send(protocol.AMEND_POSITION_SLTP_REQ, payload)
            try:
                event = await self._await(future, PROTECTION_TIMEOUT, f"amend {position_id}")
            except OrderFailed:
                event = None
        except Exception as exc:  # noqa: BLE001 - treat transport failure as protection failure
            logger.warning("%s: amend request failed: %s", self.login, exc)
            event = None
        finally:
            self._discard_watcher(future)

        if event is not None:
            if self._rejection(event):
                return False
            return True

        # No definitive event: verify against a fresh snapshot before closing
        # an otherwise healthy position.
        try:
            await self._reconcile()
        except Exception:  # noqa: BLE001
            return False
        position = self.positions.get(position_id) or {}
        got_sl = float_or(position.get("stopLoss"))
        got_tp = float_or(position.get("takeProfit"))
        sl_ok = sl <= 0 or abs(got_sl - sl) <= tolerance
        tp_ok = tp <= 0 or abs(got_tp - tp) <= tolerance
        return sl_ok and tp_ok

    # ---------- closing / cancelling ----------

    async def _close_position(self, position_id: int, reason: str = "EA") -> bool:
        position = self.positions.get(position_id)
        if position is None:
            await self._reconcile()
            position = self.positions.get(position_id)
        if position is None:
            return True
        volume = int_or((position.get("tradeData") or {}).get("volume"))
        if volume <= 0:
            return False
        stored_volume = int_or((self.persisted.get(position_id) or {}).get("bot_volume"))
        if stored_volume > 0:
            volume = min(volume, stored_volume)
        self.closing_intents[position_id] = time.monotonic()

        def predicate(message: dict) -> bool:
            if int_or(message.get("payloadType")) != protocol.EXECUTION_EVENT:
                return False
            payload = message.get("payload") or {}
            execution = execution_name(payload.get("executionType"))
            deal = payload.get("deal") or {}
            if (int_or(deal.get("positionId")) == position_id
                    and isinstance(deal.get("closePositionDetail"), dict)):
                return True
            order = payload.get("order") or {}
            if int_or(order.get("positionId")) == position_id and execution in (
                "ORDER_REJECTED", "ORDER_CANCEL_REJECTED"
            ):
                return True
            return False

        future = self._watch(predicate)
        try:
            await self._client.send(
                protocol.CLOSE_POSITION_REQ,
                {
                    "ctidTraderAccountId": self.ctid,
                    "positionId": position_id,
                    "volume": volume,
                },
            )
            try:
                event = await self._await(future, CLOSE_TIMEOUT, f"close {position_id}")
            except OrderFailed:
                event = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: close request failed: %s", self.login, exc)
            event = None
        finally:
            self._discard_watcher(future)

        if event is not None and self._rejection(event):
            self.closing_intents.pop(position_id, None)
            return False
        if event is not None:
            return True
        try:
            await self._reconcile()
        except Exception:  # noqa: BLE001
            return False
        if position_id not in self.positions:
            return True
        self.closing_intents.pop(position_id, None)
        return False

    async def _close_all_bot(self) -> None:
        for position_id in sorted(self.bot_positions):
            if self._stop.is_set() or self._restart.is_set():
                return
            try:
                await self._close_position(position_id, reason="EA")
            except Exception:  # noqa: BLE001
                logger.exception("%s: failed to close bot position %s", self.login, position_id)
            await asyncio.sleep(0.25)

    async def _cancel_order(self, order_id: int) -> bool:
        def predicate(message: dict) -> bool:
            if int_or(message.get("payloadType")) != protocol.EXECUTION_EVENT:
                return False
            payload = message.get("payload") or {}
            order = payload.get("order") or {}
            if int_or(order.get("orderId")) != order_id:
                return False
            execution = execution_name(payload.get("executionType"))
            return execution in ("ORDER_CANCELLED", "ORDER_CANCEL_REJECTED", "ORDER_REJECTED")

        future = self._watch(predicate)
        try:
            await self._client.send(
                protocol.CANCEL_ORDER_REQ,
                {"ctidTraderAccountId": self.ctid, "orderId": order_id},
            )
            try:
                event = await self._await(future, CANCEL_TIMEOUT, f"cancel {order_id}")
            except OrderFailed:
                event = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s: cancel request failed: %s", self.login, exc)
            event = None
        finally:
            self._discard_watcher(future)
        if event is not None:
            payload = event.get("payload") or {}
            if execution_name(payload.get("executionType")) == "ORDER_CANCELLED":
                self.orders.pop(order_id, None)
                self.bot_orders.discard(order_id)
                return True
            return False
        try:
            await self._reconcile()
        except Exception:  # noqa: BLE001
            return False
        return order_id not in self.orders

    # ---------- reconcile / accounting ----------

    async def _reconcile(self) -> None:
        response = await self._request(
            protocol.RECONCILE_REQ,
            {"ctidTraderAccountId": self.ctid},
            expect=protocol.RECONCILE_RES,
        )
        payload = response.get("payload") or {}
        previous_bot = set(self.bot_positions)
        new_positions: dict[int, dict] = {}
        for position in payload.get("position") or []:
            pid = int_or(position.get("positionId"))
            if pid:
                new_positions[pid] = position
        new_orders: dict[int, dict] = {}
        for order in payload.get("order") or []:
            oid = int_or(order.get("orderId"))
            if oid:
                new_orders[oid] = order

        self.positions = new_positions
        self.orders = new_orders
        self.bot_positions = {
            pid for pid, position in new_positions.items()
            if pid not in self.closed_reported and (
                self._is_bot_trade(position) or pid in previous_bot or pid in self.persisted
            )
        }
        self.bot_orders = {
            oid for oid, order in new_orders.items()
            if self._is_bot_trade(order) or oid in self.bot_orders
        }
        for pid in self.bot_positions:
            self._remember_position(pid, new_positions[pid])
        await self._backfill_entry_prices()
        self._persist_state()

        stale = (previous_bot | set(self.persisted)) - set(self.positions)
        for pid in stale:
            await self._flush_closed(pid)

    async def _flush_closed(self, position_id: int) -> None:
        if position_id in self.closed_reported:
            self._forget_position(position_id)
            return
        partial = self.partial_closes.pop(position_id, None)
        stored = self.persisted.get(position_id) or {}
        root = str(stored.get("root") or f"position{position_id}")
        symbol_id = int_or(stored.get("symbol_id"))
        lot_size = self._lot_size(symbol_id)

        if partial and int_or(partial.get("volume")) > 0:
            volume, profit = self._bot_share(
                stored, int_or(partial.get("volume")), float_or(partial.get("profit"))
            )
            await self._event(
                f"TRADE_CLOSED {root} {position_id} {partial.get('reason') or 'MANUAL'} "
                f"{lots_from_volume(volume, lot_size):.2f} {profit:.2f}"
            )
            self._mark_closed(position_id)
            return

        if not stored:
            self._forget_position(position_id)
            return

        try:
            result = await self._query_close_deals(position_id, stored)
        except Exception as exc:  # noqa: BLE001 - keep state and retry rather than lose P/L
            self.flush_attempts[position_id] = self.flush_attempts.get(position_id, 0) + 1
            logger.warning("%s: could not load closing deals for %s: %s", self.login, position_id, exc)
            if self.flush_attempts[position_id] >= 3:
                await self._event(f"ORDER_REJECTED {root} close_accounting_failed_{position_id}")
            return

        if result is None:
            self.flush_attempts[position_id] = self.flush_attempts.get(position_id, 0) + 1
            if self.flush_attempts[position_id] >= 3:
                logger.warning("%s: no closing deal found for stale position %s", self.login, position_id)
                await self._event(f"ORDER_REJECTED {root} close_accounting_failed_{position_id}")
                self._mark_closed(position_id)
            return

        volume, profit = self._bot_share(stored, result["volume"], result["profit"])
        await self._event(
            f"TRADE_CLOSED {root} {position_id} {result['reason']} "
            f"{lots_from_volume(volume, lot_size):.2f} {profit:.2f}"
        )
        self._mark_closed(position_id)

    @staticmethod
    def _bot_share(stored: dict, volume: int, profit: float) -> tuple[int, float]:
        """Cap a close to the bot's volume and prorate a netted position's P/L."""
        bot_volume = int_or(stored.get("bot_volume"))
        full_volume = int_or(stored.get("volume"))
        if bot_volume <= 0:
            return volume, profit
        if full_volume > bot_volume:
            profit *= bot_volume / full_volume
        return min(volume, bot_volume), profit

    async def _query_close_deals(self, position_id: int, stored: dict) -> dict | None:
        open_ts = int_or(stored.get("open_ts"))
        now_ms = int(time.time() * 1000)
        from_ts = max(0, open_ts - 60_000) if open_ts else max(0, now_ms - 7 * 24 * 3600 * 1000)
        response = await self._request(
            protocol.DEAL_LIST_REQ,
            {
                "ctidTraderAccountId": self.ctid,
                "fromTimestamp": from_ts,
                "toTimestamp": now_ms,
                "maxRows": 500,
            },
            expect=protocol.DEAL_LIST_RES,
        )
        payload = response.get("payload") or {}
        profit = 0.0
        volume = 0
        last_price = 0.0
        last_digits = self._digits(int_or(stored.get("symbol_id")))
        for deal in payload.get("deal") or []:
            if int_or(deal.get("positionId")) != position_id:
                continue
            detail = deal.get("closePositionDetail")
            if not isinstance(detail, dict):
                continue
            digits = digits_or(detail.get("moneyDigits"), digits_or(deal.get("moneyDigits"), self.money_digits))
            profit += net_close_profit(detail, digits)
            volume += int_or(detail.get("closedVolume"), int_or(deal.get("filledVolume")))
            last_price = float_or(deal.get("executionPrice"), last_price)
        if volume <= 0:
            return None
        position = {
            "stopLoss": stored.get("sl"),
            "takeProfit": stored.get("tp"),
            "tradeData": {"tradeSide": stored.get("side")},
        }
        reason = classify_close(
            {"executionPrice": last_price}, position, last_digits,
            initiated_by_bot=position_id in self.closing_intents,
        )
        return {"profit": profit, "volume": volume, "reason": reason}

    async def _sync_broker_history_safe(self, *, days: int) -> None:
        try:
            await self._sync_broker_history(days=days)
        except Exception as exc:  # noqa: BLE001 - history is display-only
            logger.warning("%s: broker history sync failed: %s", self.login, exc)

    async def _sync_broker_history(self, *, days: int) -> None:
        """Send broker-side closes to the backend for display only.

        GuardRails' own closes from this runtime arrive via ``TRADE_CLOSED``
        and are skipped here. Labeled deals that predate the current state (for
        example after a migration) are imported as recovered history. The
        backend stores every broker row separately so it never affects loss
        limits or block state.
        """
        now_ms = int(time.time() * 1000)
        response = await self._request(
            protocol.DEAL_LIST_REQ,
            {
                "ctidTraderAccountId": self.ctid,
                "fromTimestamp": now_ms - max(1, int(days)) * 24 * 3600 * 1000,
                "toTimestamp": now_ms,
                "maxRows": 500,
            },
            expect=protocol.DEAL_LIST_RES,
        )
        deals = (response.get("payload") or {}).get("deal") or []
        closing: list[dict] = []
        symbol_ids: set[int] = set()
        for deal in deals:
            detail = deal.get("closePositionDetail")
            if not isinstance(detail, dict):
                continue
            pid = int_or(deal.get("positionId"))
            if pid and (pid in self.closed_reported or pid in self.persisted):
                continue
            closing.append(deal)
            sid = int_or(deal.get("symbolId"))
            if sid:
                symbol_ids.add(sid)
        if not closing:
            return

        missing = [sid for sid in symbol_ids if sid not in self.book.meta_by_id]
        if missing:
            await self._fetch_metadata(missing)

        rows = []
        for deal in closing:
            detail = deal.get("closePositionDetail") or {}
            sid = int_or(deal.get("symbolId"))
            meta = self.book.meta_by_id.get(sid)
            volume = int_or(detail.get("closedVolume"), int_or(deal.get("filledVolume")))
            lots = lots_from_volume(volume, meta.lot_size) if meta else 0.0
            digits = digits_or(
                detail.get("moneyDigits"),
                digits_or(deal.get("moneyDigits"), self.money_digits),
            )
            close_ms = int_or(deal.get("createTimestamp") or deal.get("executionTimestamp"))
            labeled = str(deal.get("label") or "").startswith(LABEL_PREFIX)
            rows.append({
                "deal_id": str(deal.get("dealId") or ""),
                "position_id": str(deal.get("positionId") or ""),
                "time": close_ms / 1000.0 if close_ms > 0 else time.time(),
                "symbol": self.book.name_for_id(sid) if sid else "",
                "side": side_name(deal.get("tradeSide")),
                "volume": f"{lots:.2f}" if lots > 0 else str(volume),
                "profit": round(net_close_profit(detail, digits), 2),
                "origin": "bot" if labeled else "manual",
            })
        rows = [row for row in rows if row["deal_id"]]
        if not rows:
            return
        await self.backend.post_broker_history(self.login, rows)
        logger.info("%s: synced %d broker-side deal(s) to history", self.login, len(rows))

    async def _backfill_entry_prices(self) -> None:
        """Recover entry prices for restarted positions from deal history.

        cTrader's reconcile payload omits the fill price, so a connector
        restart (or a deployment before entry tracking existed) would leave
        the app showing 0.0 until the next trade.
        """
        missing = [
            pid for pid in self.bot_positions
            if pid not in self.entry_backfill_tried
            and float_or((self.persisted.get(pid) or {}).get("entry")) <= 0
        ]
        if not missing:
            return
        now_ms = int(time.time() * 1000)
        try:
            response = await self._request(
                protocol.DEAL_LIST_REQ,
                {
                    "ctidTraderAccountId": self.ctid,
                    "fromTimestamp": now_ms - 7 * 24 * 3600 * 1000,
                    "toTimestamp": now_ms,
                    "maxRows": 500,
                },
                expect=protocol.DEAL_LIST_RES,
            )
        except protocol.APIError as exc:
            logger.debug("%s: entry backfill failed: %s", self.login, exc)
            return
        # The request succeeded; opening deals are immutable, so do not retry
        # every reconcile for a position whose fill predates the history window.
        self.entry_backfill_tried.update(missing)
        weighted: dict[int, tuple[float, int]] = {}
        for deal in (response.get("payload") or {}).get("deal") or []:
            pid = int_or(deal.get("positionId"))
            if pid not in missing or isinstance(deal.get("closePositionDetail"), dict):
                continue
            price = float_or(deal.get("executionPrice"))
            volume = max(1, int_or(deal.get("filledVolume")))
            if price <= 0:
                continue
            total, total_volume = weighted.get(pid, (0.0, 0))
            weighted[pid] = (total + price * volume, total_volume + volume)
        for pid, (total, total_volume) in weighted.items():
            stored = self.persisted.get(pid)
            if stored is None or total_volume <= 0:
                continue
            stored["entry"] = float(
                fmt_price(total / total_volume, self._digits(int_or(stored.get("symbol_id"))))
            )
        if weighted:
            self._persist_state()

    def _remember_position(self, position_id: int, position: dict, *,
                           bot_volume: int | None = None,
                           entry: float | None = None) -> None:
        trade = position.get("tradeData") or {}
        symbol_id = int_or(trade.get("symbolId"))
        volume = int_or(trade.get("volume"))
        previous = self.persisted.get(position_id) or {}
        if bot_volume is None:
            bot_volume = int_or(previous.get("bot_volume"), volume)
        bot_volume = int_or(bot_volume)
        if volume > 0:
            # A manual partial close or a netted merge can shrink the broker
            # position; the bot's tracked volume must never exceed it.
            bot_volume = min(bot_volume, volume)
        entry_price = float_or(entry)
        if entry_price <= 0:
            entry_price = float_or(previous.get("entry"))
        if entry_price <= 0:
            entry_price = float_or(position.get("price"))
        self.persisted[position_id] = {
            "symbol_id": symbol_id,
            "root": self._symbol_root(position),
            "side": side_name(trade.get("tradeSide")),
            "volume": volume,
            # The volume this bot actually opened. On a netted account the
            # broker position may also contain manual volume, so closing and
            # reporting must never assume ``volume`` is ours.
            "bot_volume": bot_volume,
            "open_ts": int_or(trade.get("openTimestamp")),
            # Entry price for the app/status display. cTrader's reconcile
            # payload does not repeat it, so it must be captured at fill time
            # and persisted across connector restarts.
            "entry": entry_price,
            "sl": float_or(position.get("stopLoss")),
            "tp": float_or(position.get("takeProfit")),
        }
        self._persist_state()

    def _mark_closed(self, position_id: int) -> None:
        self._trader_refreshed_at = 0.0
        self.closed_reported.add(position_id)
        if len(self.closed_reported) > 2000:
            self.closed_reported = set(list(self.closed_reported)[-1000:])
        self._forget_position(position_id)

    def _forget_position(self, position_id: int) -> None:
        self.bot_positions.discard(position_id)
        self.persisted.pop(position_id, None)
        self.partial_closes.pop(position_id, None)
        self.closing_intents.pop(position_id, None)
        self.flush_attempts.pop(position_id, None)
        self.trigger_reported.discard(position_id)
        self._persist_state()

    # ---------- persisted state ----------

    def _load_state(self) -> tuple[dict[int, dict], set[int]]:
        empty: tuple[dict[int, dict], set[int]] = ({}, set())
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        except Exception:  # noqa: BLE001 - corrupt state must not stop trading
            logger.warning("%s: ignoring unreadable state file %s", self.login, self.state_path)
            return empty
        if not isinstance(raw, dict):
            return empty
        positions = raw.get("positions")
        if not isinstance(positions, dict):
            positions = {}
        result: dict[int, dict] = {}
        for key, value in positions.items():
            try:
                pid = int(key)
            except (TypeError, ValueError):
                continue
            if pid > 0 and isinstance(value, dict):
                result[pid] = value
        closed: set[int] = set()
        for value in raw.get("closed_positions") or []:
            try:
                pid = int(value)
            except (TypeError, ValueError):
                continue
            if pid > 0:
                closed.add(pid)
        return result, closed

    def _persist_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ctid_trader_account_id": self.ctid,
                "updated_ts": time.time(),
                "positions": {str(pid): info for pid, info in self.persisted.items()},
                # Survives restarts so a netted account's manual remainder is
                # never mistaken for a fresh bot position with the same id.
                "closed_positions": sorted(self.closed_reported),
            }
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            temporary.replace(self.state_path)
        except Exception:  # noqa: BLE001 - state persistence is best-effort
            logger.warning("%s: could not persist state to %s", self.login, self.state_path)

    # ---------- reporting ----------

    async def _report(self) -> None:
        if (
            not self.trader
            or time.monotonic() - self._trader_refreshed_at >= TRADER_REFRESH_SECONDS
        ):
            try:
                await self._refresh_trader()
            except Exception as exc:  # noqa: BLE001 - keep reporting the last snapshot
                logger.debug("%s: trader refresh failed: %s", self.login, exc)
        await self._refresh_pnl()
        fields = self._report_fields()
        try:
            await self.backend.post_report(self.login, fields)
        except BackendError as exc:
            logger.warning("%s: report failed: %s", self.login, exc)
            self._note_backend_failure(exc)

    async def _refresh_pnl(self) -> None:
        if not self.positions:
            self.pnl = {}
            return
        try:
            response = await self._request(
                protocol.POSITION_UNREALIZED_PNL_REQ,
                {"ctidTraderAccountId": self.ctid},
                expect=protocol.POSITION_UNREALIZED_PNL_RES,
            )
        except protocol.APIError as exc:
            logger.debug("%s: PnL refresh failed: %s", self.login, exc)
            return
        payload = response.get("payload") or {}
        digits = digits_or(payload.get("moneyDigits"), self.money_digits)
        fresh: dict[int, float] = {}
        for item in payload.get("positionUnrealizedPnL") or []:
            pid = int_or(item.get("positionId"))
            if pid:
                fresh[pid] = money(item.get("netUnrealizedPnL"), digits)
        for pid in self.positions:
            self.pnl.setdefault(pid, 0.0)
        self.pnl = fresh

    def _report_fields(self) -> dict:
        balance = self._balance()
        equity = balance + sum(self.pnl.values())
        margin = self._margin_used()
        fields: dict[str, str] = {
            "balance": f"{balance:.2f}",
            "equity": f"{equity:.2f}",
            "margin": f"{margin:.2f}",
            "freemargin": f"{equity - margin:.2f}",
            "blocked": "1" if self.blocked else "0",
        }
        for root in self.symbols:
            resolved = self._resolve_root(root)
            key = quote_key(root)
            if resolved is None:
                fields[f"{key}_ask"] = "0"
                fields[f"{key}_bid"] = "0"
                continue
            symbol_id = resolved.light.symbol_id
            digits = self._digits(symbol_id)
            quote = self.quotes.get(symbol_id) or {}
            bid = float_or(quote.get("bid"))
            ask = float_or(quote.get("ask"))
            if bid <= 0 or ask <= 0:
                fields[f"{key}_ask"] = "0"
                fields[f"{key}_bid"] = "0"
                continue
            # Keep the last known price even when the market is closed so the
            # operator sees Friday's close instead of 0. The stale flag tells
            # the backend not to treat it as tradable.
            fields[f"{key}_ask"] = fmt_price(ask, digits)
            fields[f"{key}_bid"] = fmt_price(bid, digits)
            server_ts = int_or(quote.get("server_ts"))
            if server_ts > 0:
                fields[f"{key}_ts"] = str(server_ts)
            if self._quote_age(quote) > QUOTE_REPORT_MAX_AGE:
                fields[f"{key}_stale"] = "1"

        positions = []
        for pid in sorted(self.bot_positions):
            position = self.positions.get(pid)
            if position is None:
                continue
            trade = position.get("tradeData") or {}
            symbol_id = int_or(trade.get("symbolId"))
            digits = self._digits(symbol_id)
            full_volume = int_or(trade.get("volume"))
            stored = self.persisted.get(pid) or {}
            bot_volume = int_or(stored.get("bot_volume"), full_volume)
            if full_volume > 0 and 0 < bot_volume < full_volume:
                bot_volume = min(bot_volume, full_volume)
            else:
                bot_volume = full_volume
            lots = lots_from_volume(bot_volume, self._lot_size(symbol_id))
            profit = self.pnl.get(pid, 0.0)
            if full_volume > 0 and 0 < bot_volume < full_volume:
                # Netted account: only our share of the net P/L is ours.
                profit *= bot_volume / full_volume
            side = side_name(trade.get("tradeSide"))
            entry_price = float_or(stored.get("entry"))
            if entry_price <= 0:
                entry_price = float_or(position.get("price"))
            quote = self.quotes.get(symbol_id) or {}
            # Current price marked against the close side, matching the P/L.
            current = float_or(quote.get("bid") if side == "BUY" else quote.get("ask"))
            positions.append(
                f"{self._symbol_root(position)},{pid},{side},"
                f"{lots:.2f},{profit:.2f},{fmt_price(position.get('stopLoss'), digits)},"
                f"{fmt_price(position.get('takeProfit'), digits)},"
                f"{fmt_price(entry_price, digits)},{fmt_price(current, digits)}"
            )
        fields["positions"] = "|".join(positions)

        pending = []
        for oid in sorted(self.bot_orders):
            order = self.orders.get(oid)
            if order is None:
                continue
            trade = order.get("tradeData") or {}
            symbol_id = int_or(trade.get("symbolId"))
            digits = self._digits(symbol_id)
            pending.append(
                f"{self._symbol_root(order)},{oid},{pending_type_label(order)},"
                f"{fmt_price(pending_price(order), digits)},"
                f"{fmt_price(order.get('stopLoss'), digits)},"
                f"{fmt_price(order.get('takeProfit'), digits)}"
            )
        fields["pending"] = "|".join(pending)

        # Copy-master feeds: every position and every pending order on the
        # account, including manual ones the bot does not manage. GuardRails
        # only reads these fields when the account is configured as a copy
        # master; they never change the normal position/pending lists above.
        copy_positions = []
        for pid in sorted(self.positions):
            position = self.positions[pid]
            trade = position.get("tradeData") or {}
            symbol_id = int_or(trade.get("symbolId"))
            digits = self._digits(symbol_id)
            lots = lots_from_volume(int_or(trade.get("volume")), self._lot_size(symbol_id))
            side = side_name(trade.get("tradeSide"))
            stored = self.persisted.get(pid) or {}
            entry_price = float_or(stored.get("entry"))
            if entry_price <= 0:
                entry_price = float_or(position.get("price"))
            quote = self.quotes.get(symbol_id) or {}
            current = float_or(quote.get("bid") if side == "BUY" else quote.get("ask"))
            copy_positions.append(
                f"{self._symbol_root(position)},{pid},{side},"
                f"{lots:.2f},0.00,{fmt_price(position.get('stopLoss'), digits)},"
                f"{fmt_price(position.get('takeProfit'), digits)},"
                f"{fmt_price(entry_price, digits)},{fmt_price(current, digits)}"
            )
        fields["copy_positions"] = "|".join(copy_positions)

        copy_pending = []
        for oid in sorted(self.orders):
            order = self.orders[oid]
            symbol_id = int_or((order.get("tradeData") or {}).get("symbolId"))
            digits = self._digits(symbol_id)
            copy_pending.append(
                f"{self._symbol_root(order)},{oid},{pending_type_label(order)},"
                f"{fmt_price(pending_price(order), digits)},"
                f"{fmt_price(order.get('stopLoss'), digits)},"
                f"{fmt_price(order.get('takeProfit'), digits)}"
            )
        fields["copy_pending"] = "|".join(copy_pending)
        return fields

    # ---------- plumbing ----------

    async def _request(self, payload_type: int, payload: dict, *, expect: int) -> dict:
        if self._client is None:
            raise ConnectionClosed("not connected")
        return await self._client.request(payload_type, payload, expect=expect)

    async def _event(self, message: str) -> None:
        try:
            await self.backend.post_event(self.login, message)
        except BackendError as exc:
            logger.warning("%s: event %r failed: %s", self.login, message, exc)
            self._note_backend_failure(exc)

    def _note_backend_failure(self, exc: BackendError) -> None:
        """Stop cleanly when the backend no longer knows this account."""
        if exc.status == 410 and not self._stop.is_set():
            logger.info("%s: account is no longer registered; stopping worker", self.login)
            self._stop.set()

    async def _post_grant_status(self, status: str, error: str) -> None:
        try:
            await self.backend.post_grant_status(self.grant_id, status, error)
        except BackendError as exc:
            logger.warning("%s: grant status update failed: %s", self.login, exc)

    async def _sleep_or_stop(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self._stop.is_set() and not self._restart.is_set():
            remaining = end - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(0.5, remaining))
            except asyncio.TimeoutError:
                pass

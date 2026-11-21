"""Fast candle + quote feed for the mobile chart.

MT5 is authoritative for execution, but its on-demand bridge is slow on first
paint (queue a CANDLES command, wait for the EA's file poll, then the next HTTP
poll). The chart therefore reads Dukascopy's public market feed, which is a
real venue quote stream (FX, indices, metals, crypto) and needs no API key.

Trading and risk decisions still use the EA's own quotes; this module only
supplies the chart series and the bid/ask strip.
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import time
import urllib.parse
import urllib.request
from math import isfinite

# app timeframe -> Dukascopy interval token
_INTERVALS = {
    "M1": "1MIN",
    "M5": "5MIN",
    "M15": "15MIN",
    "M30": "30MIN",
    "H1": "1HOUR",
    "H4": "4HOUR",
    "D1": "1DAY",
}

_INTERVAL_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}

# (instrument, interval, side) -> (fetched_at, bars)
CACHE: dict[tuple[str, str, str], tuple[float, list[dict]]] = {}
CACHE_TTL = 3.0        # seconds a fresh series is reused
STALE_OK = 900.0       # serve stale bars this long if the feed fails

_ALIASES = {
    "NAS100": "USATECH.IDX/USD", "US100": "USATECH.IDX/USD",
    "USTEC": "USATECH.IDX/USD", "NDX": "USATECH.IDX/USD",
    "US30": "USA30.IDX/USD", "DOW": "USA30.IDX/USD", "DJI": "USA30.IDX/USD",
    "SPX500": "USA500.IDX/USD", "SP500": "USA500.IDX/USD",
    "US500": "USA500.IDX/USD",
    "GER40": "DEU.IDX/EUR", "DE40": "DEU.IDX/EUR", "DAX": "DEU.IDX/EUR",
    "UK100": "GBR.IDX/GBP", "FTSE100": "GBR.IDX/GBP",
    "JP225": "JPN.IDX/JPY", "JPN225": "JPN.IDX/JPY",
    "XAUUSD": "XAU/USD", "GOLD": "XAU/USD",
    "XAGUSD": "XAG/USD", "SILVER": "XAG/USD",
    "BTCUSD": "BTC/USD", "ETHUSD": "ETH/USD",
}

_FIAT = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
    "CNH", "CNY", "MXN", "ZAR", "TRY", "SEK", "NOK", "DKK",
    "PLN", "HUF", "CZK", "SGD", "HKD",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_BASE = "https://freeserv.dukascopy.com/2.0/index.php"


def _root(symbol: str) -> str:
    """EURUSD.cash / EURUSD_0 / xauusd -> EURUSD / XAUUSD."""
    head = symbol.strip().upper().split(".")[0].split("_")[0]
    return re.sub(r"[^A-Z0-9]", "", head)


def instrument(symbol: str) -> str:
    """Broker root -> Dukascopy instrument (e.g. EURUSD -> EUR/USD)."""
    if "/" in symbol:
        return symbol.strip().upper()
    root = _root(symbol)
    if not root:
        return "EUR/USD"
    if root in _ALIASES:
        return _ALIASES[root]
    if len(root) == 6 and root[:3] in _FIAT and root[3:] in _FIAT:
        return f"{root[:3]}/{root[3:]}"
    return root


def _parse(text: str) -> list:
    """Dukascopy replies with JSONP: callback([...]);"""
    start, end = text.find("("), text.rfind(")")
    if start < 0 or end <= start:
        raise ValueError("malformed feed response")
    data = json.loads(text[start + 1:end])
    if not data or data[0] is None:
        raise ValueError("no data for instrument")
    return data


def _fetch_sync(ins: str, interval: str, side: str, limit: int) -> list[dict]:
    params = urllib.parse.urlencode({
        "path": "chart/json3",
        "instrument": ins,
        "offer_side": side,
        "interval": interval,
        "time_direction": "P",
        "timestamp": int(time.time() * 1000),
        "limit": limit,
        "jsonp": "cb",
    })
    req = urllib.request.Request(
        f"{_BASE}?{params}",
        headers={
            "User-Agent": _UA,
            "Referer": "https://www.dukascopy.com/",
            "Origin": "https://www.dukascopy.com",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        rows = _parse(resp.read().decode("utf-8", "replace"))

    # Rows are newest-first: [ms, open, high, low, close, volume].
    bars: list[dict] = []
    for row in reversed(rows):
        try:
            ts, o, h, l, c = int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4])
        except (IndexError, TypeError, ValueError):
            continue
        vol = 0
        if len(row) > 5:
            try:
                vol = int(float(row[5]))
            except (TypeError, ValueError):
                vol = 0
        bars.append({"time": ts // 1000, "open": o, "high": h, "low": l, "close": c, "volume": vol})
    if not bars:
        raise ValueError("no usable bars")
    return bars


async def _side(ins: str, interval: str, side: str, limit: int) -> list[dict]:
    key = (ins, interval, side)
    now = time.time()
    hit = CACHE.get(key)
    if hit and now - hit[0] <= CACHE_TTL:
        return hit[1]
    try:
        bars = await asyncio.to_thread(_fetch_sync, ins, interval, side, limit)
    except Exception:
        if hit and now - hit[0] <= STALE_OK:
            return hit[1]
        raise
    CACHE[key] = (now, bars)
    return bars


# (instrument, interval, account) -> current in-progress bar. Kept between
# requests so the live candle accumulates its high/low instead of resetting to
# a flat line on every 3s poll.
TAIL: dict[tuple[str, str, str], dict] = {}


def _apply_live_bar(
    bars: list[dict],
    bid: float | None,
    seconds: int,
    key: tuple[str, str, str],
) -> list[dict]:
    """Overlay the current in-progress candle with the live quote.

    Dukascopy publishes closed bars only, so without this the last candle stays
    frozen until the timeframe rolls over. The cached bars are copied before
    touching them; the feed remains the source for all closed history.
    """
    if not bars or bid is None or not isfinite(bid) or bid <= 0:
        return bars
    now = int(time.time())
    bucket = now - now % seconds
    last = bars[-1]
    if bucket - last["time"] > seconds * 3:
        # Feed is far behind (market closed / no fresh bars) — don't invent one.
        return bars

    out = list(bars)
    tail = TAIL.get(key)
    if tail is not None and tail["time"] != bucket and tail["time"] != last["time"]:
        tail = None

    if last["time"] >= bucket:
        # The feed already carries the current bar; merge its range with any
        # quote range seen since the bar opened.
        base = dict(tail) if tail is not None else dict(last)
        base["time"] = last["time"]
        base["open"] = float(last["open"])
        base["high"] = max(float(last["high"]), float(base.get("high", bid)), bid)
        base["low"] = min(float(last["low"]), float(base.get("low", bid)), bid)
        base["close"] = bid
        base["volume"] = last.get("volume", 0)
        out[-1] = base
        TAIL[key] = base
        return out

    if tail is not None:
        tail = dict(tail)
        tail["high"] = max(float(tail["high"]), bid)
        tail["low"] = min(float(tail["low"]), bid)
        tail["close"] = bid
    else:
        # Start the new candle at the live quote instead of the previous close
        # so a feed/broker difference never draws a fake gap wick.
        tail = {
            "time": bucket,
            "open": bid,
            "high": bid,
            "low": bid,
            "close": bid,
            "volume": 0,
        }
    out.append(tail)
    TAIL[key] = tail
    return out


async def candles(
    symbol: str,
    timeframe: str,
    count: int = 300,
    quote: dict | None = None,
    account: str = "",
) -> dict:
    tf = (timeframe or "M15").strip().upper()
    if tf not in _INTERVALS:
        tf = "M15"
    ins = instrument(symbol)
    interval = _INTERVALS[tf]
    count = max(20, min(1000, count))
    # Fixed upstream window so every caller shares one cache entry.
    fetch_limit = max(count, 300) + 5

    # Ask needs only its tail for the quote; the chart series is the bid side.
    # A wider window is fetched so the typical spread can be estimated.
    bid_bars, ask_bars = await asyncio.gather(
        _side(ins, interval, "B", fetch_limit),
        _side(ins, interval, "A", 40),
    )

    bid = bid_bars[-1]["close"]
    ask_last = ask_bars[-1]["close"]

    # Raw tick spreads from bar closes are noisy, and the newest partial bar
    # occasionally pairs a stale side (worst around the Friday close). Clamp
    # the live spread to a sane band around the recent median.
    ask_by_time = {bar["time"]: bar["close"] for bar in ask_bars}
    spreads = [
        ask_by_time[bar["time"]] - bar["close"]
        for bar in bid_bars[-40:]
        if bar["time"] in ask_by_time
    ]
    spreads = [s for s in spreads if s > 0]
    typical = statistics.median(spreads) if spreads else None

    raw = ask_last - bid
    if typical:
        spread = min(max(raw, typical * 0.5), typical * 3)
    else:
        spread = raw if raw > 0 else 0.0
    ask = round(bid + spread, 8)

    # The broker's tick is the most accurate live side for the account's own
    # symbols; Dukascopy remains the source for closed bars.
    quote_source = "feed"
    if quote:
        try:
            q_bid = float(quote.get("bid"))
            q_ask = float(quote.get("ask"))
        except (TypeError, ValueError):
            q_bid = q_ask = None
        if (
            q_bid is not None and q_ask is not None
            and isfinite(q_bid) and isfinite(q_ask)
            and q_bid > 0 and q_ask > q_bid
        ):
            bid, ask = q_bid, q_ask
            spread = ask - bid
            quote_source = "mt5"

    series = _apply_live_bar(
        bid_bars[-count:],
        bid,
        _INTERVAL_SECONDS[tf],
        (ins, interval, account or "feed"),
    )[-count:]

    return {
        "symbol": _root(symbol),
        "instrument": ins,
        "timeframe": tf,
        "candles": series,
        "bid": bid,
        "ask": ask,
        "spread": round(spread, 8),
        "last": bid,
        "source": "dukascopy",
        "quote_source": quote_source,
        "cached_at": time.time(),
    }

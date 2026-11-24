"""Parse closed-deal snapshots sent by the MT5 EA.

The EA's file bridge writes a compact ``deals`` field containing closing deals
from ``HistorySelect``. These rows are display-only: ``main.py`` stores them in
``broker_trades`` and they never enter the risk-managed ``trades`` table.
"""

from __future__ import annotations

MAX_HISTORY_ROWS = 1000
_SOURCES = {"broker", "broker_bot"}
_SIDES = {"BUY", "SELL"}


def parse_deals(raw: str) -> list[dict]:
    """Return validated deal rows from ``id,position,time,symbol,side,vol,pl,src``.

    Each row is separated by ``|``. Malformed, duplicate, or empty rows are
    dropped instead of failing the whole snapshot.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for chunk in (raw or "").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) != 8:
            continue
        deal_id, position_id, time_raw, symbol, side, volume, profit_raw, source = parts
        deal_id = deal_id.strip()
        position_id = position_id.strip()
        if not deal_id.isdigit() or not position_id.isdigit():
            continue
        if deal_id in seen:
            continue
        try:
            close_ts = float(time_raw)
            profit = float(profit_raw)
        except (TypeError, ValueError):
            continue
        if close_ts <= 0:
            continue
        side = side.strip().upper()
        if side not in _SIDES:
            continue
        symbol = symbol.strip().upper()[:32]
        if not symbol:
            continue
        source = source.strip().lower()
        if source not in _SOURCES:
            source = "broker"
        rows.append({
            "deal_id": deal_id,
            "position_id": position_id,
            "time": close_ts,
            "symbol": symbol,
            "side": side,
            "volume": volume.strip()[:32],
            "profit": profit,
            "source": source,
        })
        seen.add(deal_id)
        if len(rows) >= MAX_HISTORY_ROWS:
            break
    return rows

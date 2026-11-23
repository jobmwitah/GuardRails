"""Pure helpers for the copy-trading engine.

The engine deliberately reuses the same command lines the Discord/PWA order
paths already send to an EA or the cTrader connector:

    OPEN_MARKET BUY EURUSD 1.08000 0.5 3.25
    OPEN BUY EURUSD 1.09000 1.08000 1.12250 0.5

That means a slave keeps its own fixed phase risk, lot sizing, TP/RR,
position limit, block state and symbol list. Only the master's side, entry
and stop loss cross the account boundary.

This module has no backend imports so it can be unit-tested in isolation.
"""

from __future__ import annotations

from symbols import normalize_symbol, symbol_key

_SEPARATORS = "._#-"


def strip_suffix(symbol: str, suffix: str) -> str:
    """Remove an account's configured broker suffix from a full symbol."""
    symbol = normalize_symbol(symbol)
    suffix = (suffix or "").strip()
    if not symbol or not suffix or suffix == "0":
        return symbol
    if symbol.upper().endswith(suffix.upper()):
        return symbol[: len(symbol) - len(suffix)]
    return symbol


def base_root(symbol: str) -> str:
    """Best-effort instrument root across brokers (EURUSD.m -> EURUSD)."""
    symbol = normalize_symbol(symbol)
    for separator in _SEPARATORS:
        if separator in symbol:
            head = symbol.split(separator, 1)[0]
            if head:
                return head
    return symbol


def slave_symbol_for(master_symbol: str, master_suffix: str,
                     slave_symbols, slave_suffix: str) -> str | None:
    """Resolve the master's broker symbol to the slave's stored spelling.

    Exact matches win. Then the configured suffix is removed on both sides,
    then the instrument root before the first separator is compared. The last
    fallback accepts a slave symbol that starts with the master root followed
    by a broker decoration (``US100`` -> ``US100.cash``).
    """
    master_symbol = normalize_symbol(master_symbol)
    if not master_symbol:
        return None

    target = symbol_key(master_symbol)
    for symbol in slave_symbols:
        if symbol_key(symbol) == target:
            return symbol

    master_root = symbol_key(strip_suffix(master_symbol, master_suffix))
    for symbol in slave_symbols:
        if symbol_key(strip_suffix(symbol, slave_suffix)) == master_root:
            return symbol

    master_base = symbol_key(base_root(master_root))
    for symbol in slave_symbols:
        if symbol_key(base_root(symbol)) == master_base:
            return symbol

    for symbol in slave_symbols:
        key = symbol_key(symbol)
        if key.startswith(master_root) and len(key) > len(master_root):
            remainder = key[len(master_root):]
            if remainder[0] in _SEPARATORS or remainder == symbol_key(slave_suffix):
                return symbol
    return None


def compute_tp(side: str, entry: float, sl: float, rrr: float) -> float:
    distance = abs(entry - sl)
    return entry + rrr * distance if side == "BUY" else entry - rrr * distance


def valid_stop(side: str, entry: float, sl: float) -> bool:
    if entry <= 0 or sl <= 0:
        return False
    if side == "BUY":
        return sl < entry
    return sl > entry


def format_price(value: float) -> str:
    """Plain decimal price, matching the backend's existing command format."""
    return f"{value:.10f}".rstrip("0").rstrip(".")


def order_command(kind: str, side: str, symbol: str, entry: float, sl: float,
                  risk_pct: float, rrr: float) -> str:
    """Build the same command the manual order paths send to a terminal.

    ``kind`` is ``market`` or ``pending``. Pending orders carry the master's
    entry/SL and a TP recalculated for the slave's own RR; market orders let
    the terminal fetch its live entry and calculate TP at execution.
    """
    side = (side or "").upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if not symbol:
        raise ValueError("symbol is required")
    if risk_pct is None or risk_pct <= 0:
        raise ValueError("risk_pct must be positive")
    if sl is None or sl <= 0:
        raise ValueError("stop loss is required")

    if kind == "market":
        return (
            f"OPEN_MARKET {side} {symbol} {format_price(sl)} "
            f"{risk_pct:g} {rrr:g}"
        )

    if kind != "pending":
        raise ValueError("kind must be market or pending")
    if entry is None or entry <= 0 or not valid_stop(side, entry, sl):
        raise ValueError("invalid entry/stop-loss pair")
    tp = compute_tp(side, entry, sl, rrr)
    return (
        f"OPEN {side} {symbol} {format_price(entry)} {format_price(sl)} "
        f"{format_price(tp)} {risk_pct:g}"
    )


def pending_side(type_label: str) -> str:
    label = (type_label or "").upper()
    return "BUY" if label.startswith("BUY") else "SELL"

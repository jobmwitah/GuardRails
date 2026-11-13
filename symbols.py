"""Symbol-list helpers.

Symbols are broker-specific, so they are configured from Discord per
account instead of being hardcoded in the backend. The backend stores the
bare broker root (EURUSD, XAUUSD, US30, US100.cash, ...); the MT5 EA appends
its own ``SymbolSuffix`` at execution time.

Bars, dots and other suffixes can be case-sensitive. For example FTMO lists
``US100.cash`` and rejects ``US100.CASH``, so the user's casing is preserved.
Comparisons in the bot are case-insensitive and resolve to the stored spelling.
"""

import re

DEFAULT_SYMBOLS = ("EURUSD", "GBPUSD")
MAX_SYMBOLS_PER_ACCOUNT = 20
MAX_SYMBOL_LENGTH = 24

# Broker roots can contain dots (EURUSD.m style accounts), underscores,
# hashes (US30#) and hyphens. No spaces or slashes: those are normalised away.
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._#-]{1,23}$")
_SPLIT_RE = re.compile(r"[,\s]+")


def normalize_symbol(raw: str) -> str:
    """Trim, preserve case, and drop the slash people type in EUR/USD."""
    return (raw or "").strip().replace("/", "")


def symbol_key(raw: str) -> str:
    """Case-insensitive comparison key; display always uses stored casing."""
    return normalize_symbol(raw).upper()


def find_symbol(symbols, raw: str):
    """Return the stored spelling of ``raw`` or ``None``."""
    key = symbol_key(raw)
    if not key:
        return None
    for symbol in symbols:
        if symbol_key(symbol) == key:
            return symbol
    return None


def validate_symbol(raw: str):
    """Return ``(symbol, None)`` or ``(None, error_message)``."""
    symbol = normalize_symbol(raw)
    if not symbol:
        return None, "Symbol is empty."
    if len(symbol) > MAX_SYMBOL_LENGTH:
        return None, f"`{symbol}` is longer than {MAX_SYMBOL_LENGTH} characters."
    if not _SYMBOL_RE.fullmatch(symbol):
        return None, (
            f"`{symbol}` isn't a valid broker root. Use letters, digits, "
            "`.`, `_`, `#` or `-` — for example EURUSD, XAUUSD, US30."
        )
    return symbol, None


def parse_symbol_input(raw: str):
    """Parse a comma/space separated list into ``(symbols, errors)``."""
    valid, errors, seen = [], [], set()
    for chunk in _SPLIT_RE.split((raw or "").strip()):
        if not chunk:
            continue
        symbol, error = validate_symbol(chunk)
        if error:
            errors.append(error)
        elif symbol_key(symbol) not in seen:
            seen.add(symbol_key(symbol))
            valid.append(symbol)
    if not valid and not errors:
        errors.append("No symbols supplied.")
    return valid, errors


def quote_key(symbol: str) -> str:
    """Key the EA uses in ``/report`` for a symbol's ask/bid fields."""
    return re.sub(r"[^a-z0-9]", "_", symbol.lower())


def merge_symbols(existing, incoming):
    """Return ``(merged, added, already, errors)`` without exceeding the cap."""
    merged = list(existing)
    by_key = {symbol_key(symbol): symbol for symbol in merged}
    added, already, errors = [], [], []
    for symbol in incoming:
        key = symbol_key(symbol)
        if key in by_key:
            already.append(by_key[key])
        elif len(merged) >= MAX_SYMBOLS_PER_ACCOUNT:
            errors.append(
                f"`{symbol}` not added — limit is {MAX_SYMBOLS_PER_ACCOUNT} per account."
            )
        else:
            merged.append(symbol)
            by_key[key] = symbol
            added.append(symbol)
    return merged, added, already, errors

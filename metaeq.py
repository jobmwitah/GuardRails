"""MetaEq Manager catalog and control-file helpers.

MetaEq is a portfolio EA with eleven independent strategies. Each strategy is
pinned to exactly one broker symbol group, and the manager groups them by that
pair so a ranging-market decision can be made per pair:

    NAS100  S1, S3, S4, S8 (ranging-sensitive) + S9 (not affected)
    XAUUSD  S2B, S6        (ranging-sensitive) + none
    USDJPY  S2A, S3B, S5   (ranging-sensitive) + S7 (not affected)

The backend renders a small flat INI file. MetaEq reads it once per second and
gates only *new entries*, so disabling a strategy never abandons an open
position. A missing or corrupt file blocks every new entry (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

PAIR_ORDER = ("NAS100", "XAUUSD", "USDJPY")

# Bare broker roots. The account suffix is appended at runtime; casing is
# never changed because Pepperstone-style suffixes are case-sensitive.
PAIR_SYMBOLS = {
    "NAS100": "NAS100",
    "XAUUSD": "XAUUSD",
    "USDJPY": "USDJPY",
}

# MetaEq's magic-number bank. GlobalBaseMagic (100000) + offset. These stay
# inside EA_Bridge's ExternalMagicMin..ExternalMagicMax exemption window.
MAGIC_OFFSETS = {
    "S1": 1000,
    "S2A": 2000,
    "S3": 3000,
    "S2B": 4000,
    "S3B": 5000,
    "S4": 6000,
    "S5": 7000,
    "S6": 8000,
    "S7": 9000,
    "S8": 10000,
    "S9": 11000,
}


@dataclass(frozen=True)
class Strategy:
    name: str
    pair: str
    ranging_affected: bool
    description: str


STRATEGY_ORDER = ("S1", "S2A", "S2B", "S3", "S3B", "S4", "S5", "S6", "S7", "S8", "S9")

STRATEGIES = {
    "S1": Strategy("S1", "NAS100", True, "Intraday Volatility Breakout"),
    "S3": Strategy("S3", "NAS100", True, "Trend Pullback A"),
    "S4": Strategy("S4", "NAS100", True, "Opening Range Breakout (ORB)"),
    "S8": Strategy("S8", "NAS100", True, "NAS100 Monday"),
    "S9": Strategy("S9", "NAS100", False, "Index Fade"),
    "S2B": Strategy("S2B", "XAUUSD", True, "Multi-Symbol Crypto Edge B"),
    "S6": Strategy("S6", "XAUUSD", True, "Breakout B (gold opening range)"),
    "S2A": Strategy("S2A", "USDJPY", True, "Multi-Symbol Crypto Edge A"),
    "S3B": Strategy("S3B", "USDJPY", True, "Trend Pullback B"),
    "S5": Strategy("S5", "USDJPY", True, "Breakout A (Asian range)"),
    "S7": Strategy("S7", "USDJPY", False, "Gotobi (Tokyo fix)"),
}

# Display order inside each pair: ranging-sensitive first, then the rest.
PAIR_STRATEGIES = {
    pair: tuple(
        s.name for s in STRATEGIES.values() if s.pair == pair and s.ranging_affected
    ) + tuple(
        s.name for s in STRATEGIES.values() if s.pair == pair and not s.ranging_affected
    )
    for pair in PAIR_ORDER
}


def normalize_pair(value: str) -> str | None:
    text = (value or "").strip().upper()
    return text if text in PAIR_ORDER else None


def normalize_strategy(value: str) -> str | None:
    text = (value or "").strip().upper()
    return text if text in STRATEGIES else None


def pair_strategies(pair: str) -> tuple[str, ...]:
    canonical = normalize_pair(pair)
    return PAIR_STRATEGIES.get(canonical, ()) if canonical else ()


def affected_strategies(pair: str) -> tuple[str, ...]:
    canonical = normalize_pair(pair)
    if not canonical:
        return ()
    return tuple(s for s in PAIR_STRATEGIES[canonical] if STRATEGIES[s].ranging_affected)


def non_affected_strategies(pair: str) -> tuple[str, ...]:
    canonical = normalize_pair(pair)
    if not canonical:
        return ()
    return tuple(s for s in PAIR_STRATEGIES[canonical] if not STRATEGIES[s].ranging_affected)


def default_states() -> dict[str, bool]:
    """Every strategy starts enabled; Verified.set also has all of them on."""
    return {name: True for name in STRATEGY_ORDER}


def symbol_map(suffix: str = "0") -> dict[str, str]:
    """Bare pair root -> the exact symbol MetaEq should subscribe to."""
    text = (suffix or "0").strip()
    if text == "0":
        text = ""
    return {pair: PAIR_SYMBOLS[pair] + text for pair in PAIR_ORDER}


def resolve_symbol(pair: str, suffix: str = "0") -> str:
    canonical = normalize_pair(pair)
    if not canonical:
        return ""
    return symbol_map(suffix)[canonical]


def control_text(login: str, states: dict[str, bool], chart_symbol: str,
                 updated: datetime | None = None) -> str:
    """Render the INI MetaEq polls. Only 1/0 values are written."""
    stamp = (updated or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lines = [
        "; GuardRails MetaEq Manager control file.",
        "; Generated automatically. Manual edits are overwritten.",
        "version=1",
        f"account={login}",
        f"chart={chart_symbol}",
        f"updated={stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}",
    ]
    for name in STRATEGY_ORDER:
        lines.append(f"{name}=1" if states.get(name, False) else f"{name}=0")
    return "\r\n".join(lines) + "\r\n"


def parse_control_text(text: str) -> dict[str, bool]:
    """Test/debug helper mirroring MetaEq's fail-closed parser."""
    states = {name: False for name in STRATEGY_ORDER}
    version = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().lower()
        if key == "version":
            try:
                version = int(value)
            except ValueError:
                version = -1
        elif key in states:
            states[key] = value in ("1", "true", "yes", "on")
    return states if version == 1 else {name: False for name in STRATEGY_ORDER}

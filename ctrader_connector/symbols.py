"""Symbol metadata, quote keys, risk sizing and currency conversion.

cTrader's volume fields are in cents: a symbol's ``lotSize`` is also in cents,
so one lot is exactly ``lotSize`` protocol volume. Money fields use a
``moneyDigits`` exponent supplied by the server, so all helpers here take that
exponent explicitly instead of assuming two decimals.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


class SizingError(RuntimeError):
    def __init__(self, reason: str, **details):
        self.reason = reason
        self.details = details
        super().__init__(reason)


def quote_key(symbol: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", (symbol or "").lower())


def normalize_symbol(name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def money(value, digits: int) -> float:
    try:
        return float(value) / (10 ** int(digits))
    except (TypeError, ValueError):
        return 0.0


def price(value, digits: int = 5) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value) / 100000.0
    except (TypeError, ValueError):
        return None


def fmt_price(value, digits: int = 5) -> str:
    if value in (None, ""):
        return "0"
    try:
        return f"{float(value):.{max(0, int(digits))}f}"
    except (TypeError, ValueError):
        return "0"


def lots_from_volume(volume, lot_size) -> float:
    try:
        lot_size = int(lot_size)
        return float(volume) / lot_size if lot_size > 0 else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def volume_from_lots(lots: float, lot_size: int) -> int:
    return int(round(float(lots) * int(lot_size)))


@dataclass
class SymbolMeta:
    symbol_id: int
    name: str
    digits: int = 5
    pip_position: int = 4
    lot_size: int = 0
    min_volume: int = 0
    step_volume: int = 0
    max_volume: int = 0
    base_asset_id: int = 0
    quote_asset_id: int = 0
    enabled: bool = True
    sl_distance: int = 0
    tp_distance: int = 0
    guaranteed_stop_loss: bool = False

    @property
    def step_lots(self) -> float:
        return self.step_volume / self.lot_size if self.lot_size > 0 else 0.0

    @property
    def min_lots(self) -> float:
        return self.min_volume / self.lot_size if self.lot_size > 0 else 0.0

    @property
    def max_lots(self) -> float:
        return self.max_volume / self.lot_size if self.max_volume > 0 and self.lot_size > 0 else math.inf


@dataclass
class LightSymbol:
    symbol_id: int
    name: str
    enabled: bool = True
    base_asset_id: int = 0
    quote_asset_id: int = 0


@dataclass
class ResolvedSymbol:
    root: str
    light: LightSymbol
    meta: SymbolMeta | None = None


@dataclass
class Sizing:
    volume: int
    lots: float
    risk_per_lot: float
    actual_risk: float
    actual_risk_pct: float
    ideal_lots: float
    margin_capped: bool = False
    margin_allows_lots: float = -1.0


class SymbolBook:
    """Resolve GuardRails' bare symbol roots against a broker's symbol list."""

    _SAFE_SUFFIXES = {"CASH", "RAW", "ECN", "PRO", "SPOT", "MINI", "MICRO", "M", "Z"}

    def __init__(self, aliases: dict[str, str] | None = None):
        self.aliases = {normalize_symbol(k): v for k, v in (aliases or {}).items()}
        self.light_by_id: dict[int, LightSymbol] = {}
        self.meta_by_id: dict[int, SymbolMeta] = {}
        self.root_by_id: dict[int, str] = {}

    def add_light(self, item: dict) -> LightSymbol | None:
        symbol_id = int(item.get("symbolId") or 0)
        if not symbol_id:
            return None
        light = LightSymbol(
            symbol_id=symbol_id,
            name=str(item.get("symbolName") or f"symbol:{symbol_id}"),
            enabled=bool(item.get("enabled", True)),
            base_asset_id=int(item.get("baseAssetId") or 0),
            quote_asset_id=int(item.get("quoteAssetId") or 0),
        )
        self.light_by_id[symbol_id] = light
        return light

    def load_light(self, items: list[dict]) -> None:
        self.light_by_id.clear()
        for item in items:
            self.add_light(item)

    def load_meta(self, items: list[dict]) -> None:
        for item in items:
            symbol_id = int(item.get("symbolId") or 0)
            if not symbol_id:
                continue
            light = self.light_by_id.get(symbol_id)
            self.meta_by_id[symbol_id] = SymbolMeta(
                symbol_id=symbol_id,
                name=str(item.get("symbolName") or (light.name if light else f"symbol:{symbol_id}")),
                digits=int(item.get("digits") or 5),
                pip_position=int(item.get("pipPosition") or 4),
                lot_size=int(item.get("lotSize") or 0),
                min_volume=int(item.get("minVolume") or 0),
                step_volume=int(item.get("stepVolume") or 0),
                max_volume=int(item.get("maxVolume") or 0),
                base_asset_id=int(item.get("baseAssetId") or (light.base_asset_id if light else 0)),
                quote_asset_id=int(item.get("quoteAssetId") or (light.quote_asset_id if light else 0)),
                enabled=bool(item.get("enabled", True)),
                sl_distance=int(item.get("slDistance") or 0),
                tp_distance=int(item.get("tpDistance") or 0),
                guaranteed_stop_loss=bool(item.get("guaranteedStopLoss")),
            )

    def resolve(self, root: str) -> ResolvedSymbol | None:
        key = normalize_symbol(root)
        if not key:
            return None
        alias = self.aliases.get(key)
        candidates = list(self.light_by_id.values())
        if alias:
            alias_key = normalize_symbol(alias)
            for light in candidates:
                if normalize_symbol(light.name) == alias_key:
                    return ResolvedSymbol(root=root, light=light, meta=self.meta_by_id.get(light.symbol_id))
        # Exact broker spelling first, ignoring separators and case.
        for light in candidates:
            if normalize_symbol(light.name) == key:
                return ResolvedSymbol(root=root, light=light, meta=self.meta_by_id.get(light.symbol_id))
        # A broker may expose US100.cash / US100.raw while Discord stores US100.
        matches = []
        for light in candidates:
            norm = normalize_symbol(light.name)
            if not norm.startswith(key):
                continue
            remainder = norm[len(key):]
            if remainder in self._SAFE_SUFFIXES:
                matches.append(light)
        if len(matches) == 1:
            light = matches[0]
            return ResolvedSymbol(root=root, light=light, meta=self.meta_by_id.get(light.symbol_id))
        return None

    def bind_root(self, root: str, symbol_id: int) -> None:
        self.root_by_id[symbol_id] = root

    def name_for_id(self, symbol_id: int) -> str:
        meta = self.meta_by_id.get(symbol_id)
        if meta:
            return meta.name
        light = self.light_by_id.get(symbol_id)
        return light.name if light else f"symbol:{symbol_id}"

    def root_for_id(self, symbol_id: int) -> str:
        if symbol_id in self.root_by_id:
            return self.root_by_id[symbol_id]
        return self.name_for_id(symbol_id)

    def edges(self) -> dict[int, list[tuple[int, int, str]]]:
        """Asset -> [(symbol_id, other_asset_id, price_side)].

        These rates convert a *risk amount*, not an executed trade, so each
        side deliberately overstates the deposit-currency loss:

        ``ask`` means amount_quote = amount_base * ask (selling the base asset
        would really get bid, so ask is the conservative side). ``bid_inv``
        means amount_base = amount_quote / bid (buying the base asset would
        really cost ask, so bid is the conservative side).

        Using the side that makes the loss look bigger keeps position sizing
        from exceeding the configured risk percentage.
        """
        out: dict[int, list[tuple[int, int, str]]] = {}
        for light in self.light_by_id.values():
            base, quote = light.base_asset_id, light.quote_asset_id
            if not base or not quote or base == quote:
                continue
            out.setdefault(quote, []).append((light.symbol_id, base, "bid_inv"))
            out.setdefault(base, []).append((light.symbol_id, quote, "ask"))
        return out


def conversion_rate(from_asset: int, to_asset: int,
                    edges: dict[int, list[tuple[int, int, str]]],
                    quotes: dict[int, dict], *, max_depth: int = 4) -> float | None:
    """Conservative quote-asset -> deposit-asset conversion for risk sizing.

    Every step overstates the deposit-currency loss (see ``SymbolBook.edges``)
    and, if several paths exist, the highest resulting rate wins so sizing
    never understates the value of risk.
    """
    if not from_asset or not to_asset:
        return None
    if from_asset == to_asset:
        return 1.0
    best: float | None = None

    def walk(asset: int, rate: float, depth: int, visited: set[int]) -> None:
        nonlocal best
        if depth > max_depth:
            return
        for symbol_id, other, side in edges.get(asset, ()):
            if other in visited:
                continue
            quote = quotes.get(symbol_id)
            if not quote:
                continue
            if side == "bid_inv":
                bid = float(quote.get("bid") or 0)
                if bid <= 0:
                    continue
                step = 1.0 / bid
            else:
                ask = float(quote.get("ask") or 0)
                if ask <= 0:
                    continue
                step = ask
            next_rate = rate * step
            if next_rate <= 0:
                continue
            if other == to_asset:
                if best is None or next_rate > best:
                    best = next_rate
                continue
            walk(other, next_rate, depth + 1, visited | {other})

    walk(from_asset, 1.0, 0, {from_asset})
    return best


def size_order(*, balance: float, risk_pct: float, entry: float, stop_loss: float,
               lot_size: int, min_volume: int, step_volume: int, max_volume: int,
               quote_to_deposit: float, margin_per_lot: float | None = None,
               equity: float | None = None, margin_used: float = 0.0,
               margin_cap_pct: float = 80.0) -> Sizing:
    """Mirror the MT5 EA's risk-based sizing and 80% total-margin cap."""
    if lot_size <= 0:
        raise SizingError("symbol_metadata_incomplete")
    if step_volume <= 0:
        step_volume = min_volume if min_volume > 0 else 1
    if min_volume <= 0:
        min_volume = step_volume
    sl_distance = abs(float(entry) - float(stop_loss))
    if sl_distance <= 0:
        raise SizingError("missing_sl")
    if quote_to_deposit is None or quote_to_deposit <= 0:
        raise SizingError("conversion_unavailable")
    risk_per_lot = sl_distance * (lot_size / 100.0) * quote_to_deposit
    if risk_per_lot <= 0:
        raise SizingError("zero_lot_size")
    risk_amount = float(balance) * float(risk_pct) / 100.0
    if risk_amount <= 0:
        raise SizingError("zero_lot_size")

    ideal_lots = risk_amount / risk_per_lot
    lot_step = step_volume / lot_size
    min_lots = min_volume / lot_size
    max_lots = (max_volume / lot_size) if max_volume > 0 else math.inf

    floored = math.floor(ideal_lots / lot_step) * lot_step
    ceiled = floored + lot_step
    floored_risk = floored * risk_per_lot
    ceiled_risk = ceiled * risk_per_lot
    lots = floored
    if (ceiled_risk <= risk_amount * 1.05
            and (risk_amount - floored_risk) > (ceiled_risk - risk_amount)):
        lots = ceiled
    risk_based_lots = lots
    margin_capped = False
    margin_allows_lots = -1.0

    if margin_per_lot and margin_per_lot > 0 and equity:
        max_total_margin = float(equity) * (float(margin_cap_pct) / 100.0)
        budget = max_total_margin - float(margin_used)
        margin_allows_lots = max(0.0, budget / margin_per_lot)
        if lots > margin_allows_lots:
            lots = math.floor(margin_allows_lots / lot_step) * lot_step
        margin_capped = lots < risk_based_lots
        if margin_capped and lots < min_lots:
            raise SizingError(
                "margin_cap_below_min_lot",
                requested_lots=round(risk_based_lots, 2),
                margin_allows_lots=round(margin_allows_lots, 2),
                broker_min_lot=round(min_lots, 2),
            )

    if lots < min_lots:
        lots = min_lots
    if lots > max_lots:
        lots = max_lots
    if lots <= 0:
        raise SizingError("zero_lot_size")

    volume = volume_from_lots(lots, lot_size)
    if step_volume > 0:
        volume = (volume // step_volume) * step_volume
    if volume < min_volume:
        volume = min_volume
    if max_volume > 0 and volume > max_volume:
        volume = max_volume
    if volume <= 0:
        raise SizingError("zero_lot_size")

    actual_lots = volume / lot_size
    actual_risk = actual_lots * risk_per_lot
    actual_risk_pct = (actual_risk / balance * 100.0) if balance else 0.0
    return Sizing(
        volume=volume,
        lots=actual_lots,
        risk_per_lot=risk_per_lot,
        actual_risk=actual_risk,
        actual_risk_pct=actual_risk_pct,
        ideal_lots=ideal_lots,
        margin_capped=margin_capped,
        margin_allows_lots=margin_allows_lots,
    )

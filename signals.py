"""Tolerant parsing for inbound webhook trading signals.

Alerting platforms all post different JSON. Rather than binding GuardRails
to one vendor schema, :func:`parse_signal` flattens whatever arrives, looks
for a small set of well-known field names, and then falls back to reading
a free-text alert message such as::

    BUY EURUSD SL=1.0800
    SELL GBP/USD stop loss 1.2700
    XAUUSD LONG @ 2400, stop 2385

Parsing only extracts candidate fields. ``main.py`` remains the authority
on whether a signal is allowed to trade: account registration, symbol
whitelist, loss limits, position limits, market hours, and SL sanity are
all checked there.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from symbols import normalize_symbol, validate_symbol

BUY = "BUY"
SELL = "SELL"

# ---------------------------------------------------------------------------
# Field-name aliases (all canonicalised to lowercase letters + digits)
# ---------------------------------------------------------------------------

_ACCOUNT_KEYS = (
    "account", "accountid", "accountnumber", "login", "loginid",
    "mt5account", "mt5login", "tradingaccount", "accountlogin",
)
_EXPLICIT_SYMBOL_KEYS = (
    "symbol", "symbolname", "ticker", "tickersymbol", "pair",
    "currencypair", "instrument", "tradingsymbol", "security", "asset",
)
_LOOSE_SYMBOL_KEYS = ("market", "marketname", "epic")
_SIDE_KEYS = (
    "side", "direction", "action", "ordertype", "position", "trend", "bias",
    "condition", "alertcondition", "crossing", "operator", "comparison",
)
_SL_KEYS = (
    "sl", "stoploss", "stop", "stopprice", "stoprate", "stoplossprice",
    "slprice", "stoplevel", "stopvalue", "loss",
)
_ENTRY_KEYS = (
    "entry", "entryprice", "price", "triggerprice", "alertprice", "level",
    "rate", "close", "last", "currentprice",
)
_PLATFORM_KEYS = (
    "platform", "source", "venue", "exchange", "provider", "service", "origin",
)
_TEXT_KEYS_ORDERED = (
    "message", "text", "alertmessage", "alert", "body", "content",
    "description", "note", "notification", "msg", "subject", "title",
)
_TEXT_KEY_SET = set(_TEXT_KEYS_ORDERED)
_SECRET_KEYS = (
    "signaltoken", "bridgetoken", "authtoken", "accesstoken", "webhooktoken",
    "token", "secret", "apikey", "password", "auth", "authorization", "key",
)

# ---------------------------------------------------------------------------
# Symbol helpers
# ---------------------------------------------------------------------------

# Currency/metal/crypto bases and quotes used to recognise an unlabelled
# six-letter pair in free text. Anything outside this set has to appear in
# the account's enabled-symbol list to be picked up.
_BASES = {
    "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNH", "CNY",
    "MXN", "ZAR", "TRY", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "SGD",
    "HKD", "THB", "INR", "RUB", "BRL",
    "XAU", "XAG", "XPT", "XPD", "BTC", "ETH", "LTC", "XRP", "SOL", "DOGE",
    "ADA", "BNB",
}
_QUOTES = {
    "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNH", "CNY",
    "MXN", "ZAR", "TRY", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "SGD",
    "HKD", "THB", "INR", "RUB", "BRL",
}

_TEMPLATE_RE = re.compile(r"\{\{.*?\}\}|\{.*?\}")
_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)*")
_SL_RE = re.compile(
    r"(?i)\b(?:sl|s\s*/\s*l|stop(?:[\s-]*loss)?|stoploss)\b"
    r"\s*[:=@]?\s*([0-9]+(?:[.,][0-9]+)?)"
)
_ACCOUNT_LABELLED_RE = re.compile(
    r"(?i)\b(?:account|acct|login)\b\s*[:=#]?\s*(\d{3,12})\b"
)
_ACCOUNT_BARE_RE = re.compile(r"(?<!\d)(\d{8,12})(?!\d)")
_TOKEN_TEXT_RE = re.compile(
    r"(?i)\b(token|secret|api[_-]?key)\b\s*[:=]\s*([A-Za-z0-9._~+/=-]{6,})"
)
_BEARER_RE = re.compile(r"(?i)^bearer\s+(.+)$")


def _looks_like_symbol(symbol: str, known: set[str]) -> bool:
    """True if a validated broker root looks like an actual market symbol."""
    if symbol in known:
        return True
    if len(symbol) == 6 and symbol.isalpha():
        return symbol[:3] in _BASES and symbol[3:] in _QUOTES
    if re.fullmatch(r"[A-Z]{2,6}\d{1,4}[A-Z0-9.#_-]*", symbol):
        return True
    if re.fullmatch(r"[A-Z]{6}[._#-][A-Z0-9]+", symbol):
        return True
    return False


def _normalize_symbol_candidate(raw: str) -> str | None:
    """Turn one messy symbol field into a bare broker root, or ``None``."""
    text = str(raw or "").strip().strip("\"'`()[]{}")
    if not text or _TEMPLATE_RE.search(text):
        return None
    text = text.replace("\\/", "/")

    # "FX_OTC:EURUSD", "OANDA:EURUSD" -> "EURUSD"
    if ":" in text:
        text = text.rsplit(":", 1)[-1].strip()
    # "EURUSD=X" (Yahoo-style) -> "EURUSD"
    if "=" in text:
        text = text.split("=", 1)[0].strip()
    text = text.strip().strip("\"'`")
    if not text:
        return None

    pair = re.fullmatch(r"([A-Za-z]{3})\s*[/_.-]?\s*([A-Za-z]{3})", text)
    if pair:
        text = pair.group(1) + pair.group(2)
    else:
        text = re.split(r"\s+", text)[0]

    text = text.upper().replace("/", "")
    text = re.sub(r"^([A-Z]{3})[_.-]([A-Z]{3})$", r"\1\2", text)
    symbol, error = validate_symbol(text)
    return None if error else symbol


def _symbol_pattern(symbol: str) -> re.Pattern:
    """Regex that matches a known root even when written EUR/USD or EUR-USD."""
    separator = r"[\s/_.-]*"
    body = separator.join(re.escape(ch) for ch in symbol)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def symbol_from_text(text: str, known_symbols=()) -> str | None:
    """Find a symbol in free text, preferring the account's enabled list."""
    if not text:
        return None
    known = tuple(dict.fromkeys(normalize_symbol(s) for s in known_symbols if s))
    best: tuple[int, str] | None = None
    for symbol in known:
        match = _symbol_pattern(symbol).search(text)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), symbol)
    if best:
        return best[1]

    for match in re.finditer(
        r"(?<![A-Za-z0-9])([A-Za-z]{3})\s*[/_.-]\s*([A-Za-z]{3})(?![A-Za-z0-9])",
        text,
    ):
        candidate = (match.group(1) + match.group(2)).upper()
        if _looks_like_symbol(candidate, set(known)):
            return candidate
    for match in re.finditer(
        r"(?<![A-Za-z0-9])([A-Za-z]{6})(?![A-Za-z0-9])", text
    ):
        candidate = match.group(1).upper()
        if _looks_like_symbol(candidate, set(known)):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Side / number extraction
# ---------------------------------------------------------------------------

_BUY_TOKENS = {"buy", "long", "bull", "bullish", "bid"}
_SELL_TOKENS = {"sell", "short", "bear", "bearish", "ask"}
_BUY_UP_TOKENS = {"up", "above", "greater", "greaterthan", "crossup", "crossabove",
                  "crossingup", "crossingabove", "breakoutup"}
_SELL_DOWN_TOKENS = {"down", "below", "less", "lessthan", "crossdown",
                     "crossbelow", "crossingdown", "crossingbelow", "breakoutdown"}


def parse_side(value) -> str | None:
    """Map a side/direction/condition value to BUY or SELL."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    tokens = set(re.findall(r"[a-z]+", text))
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if compact in _BUY_UP_TOKENS | _BUY_TOKENS:
        tokens.add(compact)
    if compact in _SELL_DOWN_TOKENS | _SELL_TOKENS:
        tokens.add(compact)

    buy = bool(tokens & (_BUY_TOKENS | _BUY_UP_TOKENS))
    sell = bool(tokens & (_SELL_TOKENS | _SELL_DOWN_TOKENS))
    if buy and not sell:
        return BUY
    if sell and not buy:
        return SELL
    return None


def parse_number(value) -> float | None:
    """Tolerant number parser: 1.08, "1,0800", "SL: 1.0800", 1,085.50."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER_RE.search(str(value).replace(" ", ""))
    if not match:
        return None
    token = match.group(0)
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        token = token.replace(",", ".")
    try:
        return float(token)
    except ValueError:
        return None


def parse_sl(text: str) -> float | None:
    match = _SL_RE.search(text or "")
    return parse_number(match.group(1)) if match else None


def parse_account(text: str) -> str | None:
    match = _ACCOUNT_LABELLED_RE.search(text or "")
    if match:
        return match.group(1)
    match = _ACCOUNT_BARE_RE.search(text or "")
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Payload flattening
# ---------------------------------------------------------------------------

def _canonical_key(key) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _looks_like_json(text: str) -> bool:
    stripped = text.strip()
    return len(stripped) > 1 and stripped[0] in "{[" and stripped[-1] in "}]"


def _collect(payload):
    """Return ``(entries, texts)`` from an arbitrarily nested payload.

    ``entries`` are ``(depth, order, canonical_key, value)``. ``texts`` are
    ``(priority, depth, order, canonical_key, value)`` where priority 0 means
    the value came from a known message field. Nested JSON strings are
    expanded so a payload such as ``{"payload": "{\"symbol\":...}"}`` works.
    """
    entries: list[tuple[int, int, str, object]] = []
    texts: list[tuple[int, int, int, str, str]] = []
    counter = 0

    def walk(obj, depth: int):
        nonlocal counter
        if isinstance(obj, dict):
            for key, value in obj.items():
                norm = _canonical_key(key)
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
                elif isinstance(value, str):
                    counter += 1
                    entries.append((depth, counter, norm, value))
                    if _looks_like_json(value):
                        try:
                            walk(json.loads(value), depth + 1)
                            continue
                        except (ValueError, TypeError):
                            pass
                    texts.append(
                        (0 if norm in _TEXT_KEY_SET else 1, depth, counter, norm, value)
                    )
                else:
                    counter += 1
                    entries.append((depth, counter, norm, value))
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)
        elif isinstance(obj, str):
            counter += 1
            entries.append((depth, counter, "", obj))
            texts.append((1, depth, counter, "", obj))

    walk(payload, 0)
    return entries, texts


def _flat_fields(entries) -> dict:
    """Shallowest value per canonical key (top-level fields win)."""
    flat = {}
    for _, _, key, value in sorted(entries, key=lambda item: (item[0], item[1])):
        if key and key not in flat:
            flat[key] = value
    return flat


def _values_by_keys(entries, keys) -> list:
    """All values for the given aliases, best key first, then shallowest."""
    rank = {key: index for index, key in enumerate(keys)}
    found = [
        (rank[key], depth, order, value)
        for depth, order, key, value in entries
        if key in rank
    ]
    found.sort()
    return [value for _, _, _, value in found]


def _text_value(value) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def _first_text(flat: dict, keys) -> str:
    for key in keys:
        text = _text_value(flat.get(key))
        if text:
            return text
    return ""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class ParsedSignal:
    account: str | None = None
    symbol: str | None = None
    side: str | None = None
    sl: float | None = None
    entry: float | None = None
    message: str = ""
    platform: str = ""
    payload_digest: str = ""
    conflicts: list[str] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        missing = []
        if not self.side:
            missing.append("side")
        if not self.symbol:
            missing.append("symbol")
        if self.sl is None or self.sl <= 0:
            missing.append("sl")
        return missing

    @property
    def ok(self) -> bool:
        return not self.missing and not self.conflicts

    def event_key(self, account: str | None = None) -> str:
        """Stable content hash used for the 5-minute duplicate window.

        Deliberately does not use a vendor alert ID: those are often static
        per alert and would suppress every future firing of the same alert.
        """
        sl_text = "" if self.sl is None else f"{self.sl:.10f}".rstrip("0").rstrip(".")
        material = "|".join((
            account or self.account or "",
            self.platform or "",
            self.symbol or "",
            self.side or "",
            sl_text,
        ))
        if self.message:
            material += "|" + re.sub(r"\s+", " ", self.message.strip().lower())
        elif not (self.symbol or self.side or self.sl is not None):
            # Nothing usable was extracted. Include the payload digest so two
            # different malformed webhooks don't suppress each other, while a
            # retry of the same malformed webhook still dedupes.
            material += "|" + self.payload_digest
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _payload_digest(payload) -> str:
    try:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_signal(payload, known_symbols=()) -> ParsedSignal:
    """Extract account/symbol/side/SL from any webhook payload or text."""
    known = tuple(dict.fromkeys(normalize_symbol(s) for s in known_symbols if s))
    entries, texts = _collect(payload)
    flat = _flat_fields(entries)

    parsed = ParsedSignal(
        platform=_first_text(flat, _PLATFORM_KEYS),
        payload_digest=_payload_digest(payload),
    )

    # ---- structured fields -------------------------------------------------
    for value in _values_by_keys(entries, _ACCOUNT_KEYS):
        text = _text_value(value)
        if text and re.fullmatch(r"\d{3,12}", text):
            parsed.account = text
            break

    for value in _values_by_keys(entries, _EXPLICIT_SYMBOL_KEYS):
        symbol = _normalize_symbol_candidate(_text_value(value) or "")
        if symbol and _looks_like_symbol(symbol, set(known)):
            parsed.symbol = symbol
            break
    if parsed.symbol is None:
        for value in _values_by_keys(entries, _LOOSE_SYMBOL_KEYS):
            symbol = _normalize_symbol_candidate(_text_value(value) or "")
            if symbol and _looks_like_symbol(symbol, set(known)):
                parsed.symbol = symbol
                break

    for value in _values_by_keys(entries, _SIDE_KEYS):
        side = parse_side(value)
        if side:
            parsed.side = side
            break

    for value in _values_by_keys(entries, _SL_KEYS):
        sl = parse_number(value)
        if sl is not None and sl > 0:
            parsed.sl = sl
            break

    for value in _values_by_keys(entries, _ENTRY_KEYS):
        entry = parse_number(value)
        if entry is not None and entry > 0:
            parsed.entry = entry
            break

    # ---- free-text fallback ------------------------------------------------
    text_items: list[tuple[str, str]] = []
    for _, _, _, norm, value in sorted(
        texts, key=lambda item: (item[0], item[1], item[2])
    ):
        cleaned = value.strip()
        if cleaned and cleaned not in (text for _, text in text_items):
            text_items.append((norm, cleaned))

    best_message = ""
    best_score = 0
    for norm, text in text_items:
        text_side = parse_side(text)
        text_sl = parse_sl(text)
        text_symbol = symbol_from_text(text, known)

        if text_side:
            if parsed.side is None:
                parsed.side = text_side
            elif parsed.side != text_side:
                parsed.conflicts.append(
                    f"side: payload says {parsed.side}, message says {text_side}"
                )
        if text_sl is not None and text_sl > 0:
            if parsed.sl is None:
                parsed.sl = text_sl
            elif abs(parsed.sl - text_sl) > 1e-9:
                parsed.conflicts.append(
                    f"sl: payload says {parsed.sl}, message says {text_sl}"
                )
        if text_symbol:
            if parsed.symbol is None:
                parsed.symbol = text_symbol
            elif parsed.symbol != text_symbol:
                parsed.conflicts.append(
                    f"symbol: payload says {parsed.symbol}, message says {text_symbol}"
                )
        if parsed.account is None:
            parsed.account = parse_account(text)

        # Choose the richest message candidate, not just the first field that
        # happens to contain the word "sell" (a bare side field, for example).
        # A symbol-only structured field is not a message.
        score = 2 * bool(text_symbol) + 2 * (text_sl is not None) + bool(text_side)
        message_like = norm in _TEXT_KEY_SET or (
            text_symbol and (text_side or text_sl is not None)
        )
        if (
            message_like
            and (text_side or text_sl is not None or text_symbol)
            and score > best_score
        ):
            best_score = score
            best_message = text

        if parsed.entry is None:
            match = re.search(
                r"(?i)\b(?:entry|@|at)\s*[:=]?\s*([0-9]+(?:[.,][0-9]+)?)", text
            )
            if match:
                parsed.entry = parse_number(match.group(1))

    if best_message:
        parsed.message = best_message
    else:
        for priority, _, _, norm, value in sorted(
            texts, key=lambda item: (item[0], item[1], item[2])
        ):
            if priority == 0 and norm in _TEXT_KEY_SET:
                cleaned = value.strip()
                if cleaned:
                    parsed.message = cleaned
                    break

    # De-duplicate conflict messages while preserving order.
    parsed.conflicts = list(dict.fromkeys(parsed.conflicts))
    return parsed


# ---------------------------------------------------------------------------
# Request body / auth helpers
# ---------------------------------------------------------------------------

def decode_body(body: bytes, content_type: str = ""):
    """Decode a webhook body into JSON data, form fields, or a text message."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    ctype = (content_type or "").lower()

    if "application/x-www-form-urlencoded" in ctype:
        return {
            key: values[0]
            for key, values in parse_qs(text, keep_blank_values=True).items()
        }
    if "json" in ctype or text[:1] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if "json" in ctype:
                return {"message": text}
    return {"message": text}


def extract_token(payload=None, query=None, headers=None) -> str | None:
    """Find the shared secret in a query string, header, JSON field or text."""
    if query is not None:
        for key in ("signal_token", "token", "key", "secret"):
            value = query.get(key)
            if value:
                return str(value).strip()
    if headers is not None:
        lowered = {str(key).lower(): value for key, value in headers.items()}
        for key in ("x-signal-token", "x-bridge-token", "x-api-key"):
            value = lowered.get(key)
            if value:
                return str(value).strip()
        auth = str(lowered.get("authorization", "")).strip()
        match = _BEARER_RE.match(auth)
        if match:
            return match.group(1).strip()

    entries, texts = _collect(payload if payload is not None else {})
    for value in _values_by_keys(entries, _SECRET_KEYS):
        text = _text_value(value)
        if not text:
            continue
        match = _BEARER_RE.match(text)
        return match.group(1).strip() if match else text

    for _, _, _, _, value in sorted(texts, key=lambda item: (item[0], item[1], item[2])):
        match = _TOKEN_TEXT_RE.search(value)
        if match:
            return match.group(2)
    return None


def redact_text(text: str, secrets=()) -> str:
    """Remove known tokens and ``TOKEN=...`` patterns from a string."""
    cleaned = str(text or "")
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(str(secret), "***")
    return _TOKEN_TEXT_RE.sub(lambda match: f"{match.group(1)}=***", cleaned)


def redact_payload(payload, secrets=()):
    """Recursively redact secret-looking fields and known token values."""
    secret_values = tuple(str(s) for s in secrets if s)

    def scrub(value):
        if isinstance(value, dict):
            result = {}
            for key, child in value.items():
                canon = _canonical_key(key)
                if canon in _SECRET_KEYS:
                    if canon in {"key", "auth", "code"}:
                        # "key" is sometimes a symbol field; only redact it
                        # when it looks like a real secret.
                        child_text = str(child)
                        if not (
                            any(secret in child_text for secret in secret_values)
                            or (
                                isinstance(child, str)
                                and len(child) >= 20
                                and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", child)
                            )
                        ):
                            result[key] = scrub(child)
                            continue
                    result[key] = "***"
                else:
                    result[key] = scrub(child)
            return result
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, str):
            return redact_text(value, secret_values)
        return value

    return scrub(payload)

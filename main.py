"""
Backend for the Discord-controlled MT5 bot — multi-account, persistent version.

Runs two things concurrently:
  - A FastAPI server the EA(s) talk to (POST /report, GET /commands, POST /event)
  - A Discord bot you talk to (slash commands)

State (accounts, rule progress, trade history, default account) is persisted
to a local SQLite file (storage.py) so a backend restart doesn't lose it.

DEFAULT ACCOUNT: use /setdefault once — after that, account is optional on
every other command and falls back to it.

HARDCODED — not changeable via any Discord command, by design:
  CHALLENGE_RISK_PCT / FUNDED_RISK_PCT   risk % per trade, fixed by account phase
  REMOVE_COOLDOWN                        /removeaccount stays locked until the next Nairobi midnight after a loss
  MAX_LOSSES_PER_DAY                      trading blocks for the day after this many losses
  MAX_OPEN_POSITIONS                      max concurrent open positions per account
  Rule defaults (daily loss / drawdown / etc.)  set once via .env, no /setrule command

SYMBOLS: each account has its own enabled symbol list, managed from Discord
with /addsymbol, /removesymbol and /symbols. New accounts start with
EURUSD and GBPUSD; the MT5 EA appends its configured SymbolSuffix.

ENV VARS:
  BRIDGE_TOKEN         required; may list several comma/space-separated tokens
                       during rotation (new first), e.g. "new-token,old-token"
  DISCORD_TOKEN         required
  OWNER_DISCORD_ID      required
  ALERT_CHANNEL_ID      required
  GUILD_ID              optional — instant command sync while testing
  PUBLIC_BASE_URL       public HTTPS URL used for one-time setup links
  AGENT_BACKEND_URL     optional URL the Windows agent writes into new MT5
                        instances (defaults to PUBLIC_BASE_URL). Set it to the
                        local end of an SSH forward, e.g. http://127.0.0.1:8000,
                        so EA polling never consumes the public tunnel's quota
  CREDENTIAL_KEY        Fernet key used to encrypt provisioning payloads
  AGENT_TOKEN           shared secret for the Windows provisioning agent
  DAILY_LOSS_LIMIT / MAX_DRAWDOWN_PCT / MAX_LOT_SIZE / MAX_POSITIONS   optional, default 0 (disabled)
  DB_PATH               optional, default bot_data.db
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Literal, Optional
from urllib.parse import parse_qs

from dotenv import load_dotenv
load_dotenv()

import discord
from discord import app_commands
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi import FastAPI, Request, HTTPException
import uvicorn

import ctrader_oauth
from rules import RuleConfig, RuleState, evaluate
from symbols import (
    DEFAULT_SYMBOLS,
    MAX_SYMBOLS_PER_ACCOUNT,
    find_symbol,
    merge_symbols,
    parse_symbol_input,
    quote_key,
    validate_symbol,
)
import provisioning
import storage as db


def _token_list(value: str) -> tuple[str, ...]:
    """BRIDGE_TOKEN may list several tokens (comma/space separated) so a
    token can be rotated without breaking an EA that is still using the old
    one. Put the new token first; remove the old one once the EA is updated."""
    return tuple(token for token in re.split(r"[,;\s]+", value or "") if token)


_BRIDGE_TOKENS = _token_list(os.environ.get("BRIDGE_TOKEN", ""))
CTRADER_CONNECTOR_TOKEN = os.environ.get("CTRADER_CONNECTOR_TOKEN", "").strip()
# Short-lived HttpOnly cookie that ties the OAuth callback to the one-time
# /addctrader link even if cTrader does not echo an OAuth ``state``.
CTRADER_COOKIE = "ctrader_connect"


def connector_token_ok(supplied) -> bool:
    """Shared secret used only by the local cTrader connector service."""
    if not CTRADER_CONNECTOR_TOKEN or not supplied:
        return False
    return secrets.compare_digest(
        str(supplied).encode("utf-8"), CTRADER_CONNECTOR_TOKEN.encode("utf-8")
    )


def bridge_token_ok(supplied, account: str = "") -> bool:
    if not supplied:
        return False
    candidate = str(supplied)
    # The cTrader connector speaks the same /report, /commands and /event
    # contract as the MT5 EA, but it is a single VPS service serving every
    # cTrader account. It authenticates with its own shared secret rather than
    # a per-account bridge token, and that secret is never accepted for MT5
    # accounts. The reverse also holds: MT5 bridge tokens are not valid for
    # cTrader accounts.
    if account.startswith("ctrader:"):
        return connector_token_ok(candidate)
    if account:
        expected_hash = db.get_bridge_token_hash(account)
        if expected_hash and provisioning.bridge_token_matches(candidate, expected_hash):
            return True
    candidate_bytes = candidate.encode("utf-8")
    return any(
        secrets.compare_digest(candidate_bytes, token.encode("utf-8"))
        for token in _BRIDGE_TOKENS
    )


def _connector_auth(request: Request) -> bool:
    token = request.headers.get("x-connector-token") or request.query_params.get("token", "")
    return connector_token_ok(token)


DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_DISCORD_ID", "0"))
ALERT_CHANNEL_ID = int(os.environ.get("ALERT_CHANNEL_ID", "0") or "0")
GUILD_ID = os.environ.get("GUILD_ID")

# --- Mobile PWA ---
MOBILE_API_TOKEN = os.environ.get("MOBILE_API_TOKEN", "").strip()
_MOBILE_CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "MOBILE_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if origin.strip()
]
# Accounts the mobile app may view but never trade. The FTMO terminal
# (1515282832) is protected by default and may only be traded if the operator
# explicitly opts it back in via MOBILE_TRADE_ACCOUNTS.
_MOBILE_READ_ONLY = {
    login.strip()
    for login in os.environ.get("MOBILE_READ_ONLY_ACCOUNTS", "1515282832").split(",")
    if login.strip()
}
_MOBILE_TRADE_ACCOUNTS = {
    login.strip()
    for login in os.environ.get("MOBILE_TRADE_ACCOUNTS", "").split(",")
    if login.strip()
}

# --- Hardcoded, not exposed to any Discord command ---
CHALLENGE_RISK_PCT = 1.0
FUNDED_RISK_PCT = 0.5
MAX_LOSSES_PER_DAY = 2
MAX_OPEN_POSITIONS = 2  # once this many positions are open on an account, no more are allowed until one closes
RISK_REWARD_RATIO = 3.25  # TP is always calculated at this multiple of the SL distance


def compute_tp(direction: str, entry: float, sl: float) -> float:
    risk_distance = abs(entry - sl)
    return entry + RISK_REWARD_RATIO * risk_distance if direction == "BUY" else entry - RISK_REWARD_RATIO * risk_distance


def format_price(value: float) -> str:
    """Plain decimal price for the EA command line and Discord messages."""
    return f"{value:.10f}".rstrip("0").rstrip(".")


_ACCESS_SECRET_RE = re.compile(
    r"(?i)((?:bridge[_-]?)?token|secret|api[_-]?key)=([^&\s\"']+)"
)


def _redact_access_text(value):
    return _ACCESS_SECRET_RE.sub(r"\1=***", value) if isinstance(value, str) else value


def install_access_log_redaction():
    """Keep ?token=... out of uvicorn's access log."""
    class TokenRedactionFilter(logging.Filter):
        def filter(self, record):
            record.msg = _redact_access_text(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_access_text(arg) for arg in record.args)
            elif isinstance(record.args, dict):
                record.args = {
                    key: _redact_access_text(value)
                    for key, value in record.args.items()
                }
            return True

    logging.getLogger("uvicorn.access").addFilter(TokenRedactionFilter())


def default_rule_config() -> RuleConfig:
    return RuleConfig(
        daily_loss_limit=float(os.environ.get("DAILY_LOSS_LIMIT", "0") or "0"),
        max_drawdown_pct=float(os.environ.get("MAX_DRAWDOWN_PCT", "0") or "0"),
        max_lot_size=float(os.environ.get("MAX_LOT_SIZE", "0") or "0"),
        max_positions=int(float(os.environ.get("MAX_POSITIONS", "0") or "0")),
    )


NAIROBI_OFFSET = timedelta(hours=3)  # EAT is fixed UTC+3, no DST — safe to hardcode


def today_str() -> str:
    """Nairobi-local date, used as the boundary for all daily resets (loss count,
    daily $ loss limit, drawdown baseline). Computed as a fixed +3h shift from UTC
    rather than via zoneinfo, since Windows doesn't ship an IANA tz database by
    default and EAT has no DST to worry about anyway."""
    return (datetime.now(timezone.utc) + NAIROBI_OFFSET).date().isoformat()


app = FastAPI()


from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

if _MOBILE_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_MOBILE_CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "ngrok-skip-browser-warning"],
    )


@dataclass
class AccountState:
    login: str
    phase: Optional[str] = None
    risk_pct: Optional[float] = None
    suffix: str = "0"
    server: str = ""
    instance_status: str = "unprovisioned"
    instance_dir: str = ""
    provision_error: str = ""
    agent_id: str = ""
    bridge_token_hash: str = ""
    last_report_ts: float = 0.0
    platform: str = "mt5"
    external_id: str = ""
    display_name: str = ""
    account_environment: str = ""
    account_type: str = ""
    grant_id: int = 0
    symbols: list[str] = field(default_factory=lambda: list(DEFAULT_SYMBOLS))
    rule_config: RuleConfig = field(default_factory=default_rule_config)
    rule_state: RuleState = field(default_factory=RuleState)
    queue: list = field(default_factory=list)
    last_report: dict = field(default_factory=dict)
    trade_log: list = field(default_factory=list)
    last_loss_time: Optional[float] = None
    last_reset_date: str = ""
    daily_loss_count: int = 0
    loss_limit_alerted: bool = False
    recent_closed_tickets: dict = field(default_factory=dict)  # ticket -> ts, backend-side dedup


def is_blocked(acc: "AccountState") -> bool:
    # Authoritative on our own persisted count first — the EA's self-reported
    # "blocked" flag is just its in-memory state, which resets to false on any
    # EA restart (recompile, terminal restart) with no way for it to know on
    # its own that today's limit was already hit before it came back up.
    if acc.daily_loss_count >= MAX_LOSSES_PER_DAY:
        return True
    return bool(acc.last_report) and acc.last_report.get("blocked") == "1"


def is_market_closed_for(acc: "AccountState", symbol: str) -> bool:
    """True only when we have explicit zero-quote data for this symbol — fails
    open (returns False) if the EA hasn't reported that field yet, so a
    stale/missing field never blocks a trade that might actually be fine."""
    r = acc.last_report
    if not r:
        return False
    key = quote_key(symbol)
    key_ask, key_bid = f"{key}_ask", f"{key}_bid"
    if key_ask not in r or key_bid not in r:
        return False
    try:
        ask, bid = float(r[key_ask]), float(r[key_bid])
    except (TypeError, ValueError):
        return False
    return ask <= 0 or bid <= 0


def resolve_trade_symbol(acc: "AccountState", raw: str):
    """Validate a Discord symbol against the account's enabled list.

    Matching is case-insensitive, but the stored broker spelling is returned
    so a case-sensitive suffix such as ``US100.cash`` stays correct.
    """
    symbol, error = validate_symbol(raw)
    if error:
        return None, error
    stored = find_symbol(acc.symbols, symbol)
    if stored is None:
        return None, (
            f"`{symbol}` isn't enabled on `{acc.login}`. "
            f"Add it with `/addsymbol symbols:{symbol}`."
        )
    return stored, None


def is_at_position_limit(acc: "AccountState") -> bool:
    """True if the account already has MAX_OPEN_POSITIONS open positions.
    Fails open (False) if we have no report data yet."""
    r = acc.last_report
    if not r:
        return False
    return len(parse_positions(r.get("positions", ""))) >= MAX_OPEN_POSITIONS


accounts: dict[str, AccountState] = {}
default_account: Optional[str] = None


def persist_account(acc: AccountState):
    db.upsert_account(
        acc.login, acc.phase, acc.risk_pct, acc.suffix,
        acc.rule_state.day_start_balance, acc.rule_state.peak_equity,
        acc.rule_state.already_blocked, acc.last_loss_time,
        acc.last_reset_date, acc.daily_loss_count,
        acc.server, acc.instance_status, acc.instance_dir, acc.provision_error,
        acc.agent_id, acc.bridge_token_hash, acc.last_report_ts,
        acc.platform, acc.external_id, acc.display_name, acc.account_environment,
        acc.account_type, acc.grant_id,
    )


def load_state():
    """Called once at startup — repopulates accounts, trade_log, and default_account from disk."""
    global default_account
    for row in db.load_accounts():
        acc = AccountState(login=row["login"])
        acc.phase = row["phase"]
        acc.risk_pct = row["risk_pct"]
        acc.suffix = row["suffix"] or "0"
        acc.server = row.get("server") or ""
        acc.instance_status = row.get("instance_status") or "unprovisioned"
        acc.instance_dir = row.get("instance_dir") or ""
        acc.provision_error = row.get("provision_error") or ""
        acc.agent_id = row.get("agent_id") or ""
        acc.bridge_token_hash = row.get("bridge_token_hash") or ""
        acc.last_report_ts = float(row.get("last_report_ts") or 0)
        acc.platform = row.get("platform") or "mt5"
        acc.external_id = row.get("external_id") or ""
        acc.display_name = row.get("display_name") or ""
        acc.account_environment = row.get("account_environment") or ""
        acc.account_type = row.get("account_type") or ""
        acc.grant_id = int(row.get("grant_id") or 0)
        acc.rule_state.day_start_balance = row["day_start_balance"] or 0
        acc.rule_state.peak_equity = row["peak_equity"] or 0
        acc.rule_state.already_blocked = bool(row["already_blocked"])
        acc.last_loss_time = row["last_loss_time"]
        acc.last_reset_date = row["last_reset_date"] or ""
        acc.daily_loss_count = row["daily_loss_count"] or 0
        acc.symbols = db.load_symbols(acc.login)
        if not acc.symbols:
            # No stored list means this account predates symbol management
            # (or was created by a report before /addaccount). Seed the old
            # hardcoded defaults; removing the last symbol is refused, so an
            # empty list can never mean "the user disabled everything".
            acc.symbols = list(DEFAULT_SYMBOLS)
            db.add_symbols(acc.login, acc.symbols)
        accounts[acc.login] = acc

    for t in db.load_trades():
        acc = accounts.get(t["login"])
        if acc:
            acc.trade_log.append({
                "time": t["time"], "symbol": t["symbol"], "ticket": t["ticket"],
                "reason": t["reason"], "volume": t["volume"], "profit": t["profit"],
            })

    default_account = db.get_setting("default_account")
    print(f"Loaded {len(accounts)} account(s) from storage. Default: {default_account}")


def get_or_create(login: str) -> AccountState:
    if login not in accounts:
        acc = AccountState(login=login)
        if login.startswith("ctrader:"):
            acc.platform = "ctrader"
            acc.external_id = login.split(":", 1)[1]
            acc.instance_status = "external"
        accounts[login] = acc
    return accounts[login]


def bridge_account(login: str) -> Optional[AccountState]:
    """Resolve an account for a bridge request.

    MT5 keeps the legacy tolerance for an EA that reports before it was
    registered. A cTrader login must already exist: the connector only reports
    accounts the backend handed it, so an unknown ``ctrader:`` login means the
    account was just removed (or the request is bogus). Re-creating it here
    would resurrect a deleted account on its next report.
    """
    if login.startswith("ctrader:"):
        return accounts.get(login)
    return get_or_create(login)


def resolve_account(account: Optional[str]) -> Optional[str]:
    return account or default_account


def maybe_reset_day(acc: AccountState):
    """Rolls the account's daily counters over at Nairobi (EAT) midnight."""
    today = today_str()
    if acc.last_reset_date != today:
        acc.last_reset_date = today
        acc.rule_state.day_start_balance = 0   # re-baselines itself on the next report
        acc.rule_state.already_blocked = False
        acc.daily_loss_count = 0
        acc.loss_limit_alerted = False
        # The backend's own bookkeeping just reset, but the EA's in-memory
        # tradingBlocked flag doesn't know the day rolled over — nothing else
        # tells it to. Without this, the EA stays blocked forever after its
        # first block, since BLOCK was the only command ever sent for it.
        acc.queue.append("UNBLOCK")
        persist_account(acc)


def is_in_losing_period(acc: AccountState) -> bool:
    """Locked only for the rest of the Nairobi calendar day the last loss happened
    on — clears automatically at the next Nairobi midnight, same boundary as the
    daily loss-count/rule resets. Not a rolling 24h timer."""
    if not acc.last_loss_time:
        return False
    loss_date = (datetime.fromtimestamp(acc.last_loss_time, tz=timezone.utc) + NAIROBI_OFFSET).date().isoformat()
    return loss_date == today_str()


def removal_unlocks_at(acc: AccountState) -> Optional[str]:
    """Human-readable UTC timestamp for the next Nairobi midnight after the last
    loss — when the removal lock clears — or None if not currently locked."""
    if not is_in_losing_period(acc):
        return None
    loss_date = (datetime.fromtimestamp(acc.last_loss_time, tz=timezone.utc) + NAIROBI_OFFSET).date()
    # Nairobi midnight (EAT, UTC+3) falls at 21:00 UTC the day before
    next_reset_utc = datetime(loss_date.year, loss_date.month, loss_date.day, 21, 0, tzinfo=timezone.utc)
    return next_reset_utc.strftime("%Y-%m-%d %H:%M UTC") + " (next Nairobi midnight)"


def parse_form(body: bytes) -> dict:
    raw = parse_qs(body.decode())
    return {k: v[0] for k, v in raw.items()}


def parse_positions(raw: str) -> list[dict]:
    out = []
    for chunk in (c for c in raw.split("|") if c):
        parts = chunk.split(",")
        if len(parts) < 5:
            continue
        entry = {"symbol": parts[0], "ticket": parts[1], "type": parts[2],
                  "volume": parts[3], "profit": parts[4]}
        if len(parts) >= 7:
            entry["sl"] = parts[5]
            entry["tp"] = parts[6]
        if len(parts) >= 9:
            entry["entry"] = parts[7]
            entry["price"] = parts[8]
        out.append(entry)
    return out


def parse_pending(raw: str) -> list[dict]:
    out = []
    for chunk in (c for c in raw.split("|") if c):
        parts = chunk.split(",")
        if len(parts) < 6:
            continue
        out.append({
            "symbol": parts[0], "ticket": parts[1], "type": parts[2],
            "price": parts[3], "sl": parts[4], "tp": parts[5],
        })
    return out


def symbol_has_exposure(acc: "AccountState", symbol: str) -> bool:
    """True if the account has an open position or pending order whose broker
    symbol starts with this root (the EA reports symbols with the suffix)."""
    r = acc.last_report
    if not r:
        return False
    root = symbol.upper()
    for p in parse_positions(r.get("positions", "")) + parse_pending(r.get("pending", "")):
        if p["symbol"].upper().startswith(root):
            return True
    return False


STALE_REPORT_SECONDS = 120  # alert if an account hasn't reported in this long


async def watchdog_loop():
    """Warns if an account's EA stops reporting — closed terminal, dropped connection, etc."""
    stale_alerted: set[str] = set()
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for login, acc in accounts.items():
            last_ts = float(acc.last_report.get("_received_at", 0)) if acc.last_report else 0
            if not last_ts:
                continue
            stale = (now - last_ts) > STALE_REPORT_SECONDS
            if stale and login not in stale_alerted:
                stale_alerted.add(login)
                target = (
                    "check the cTrader connector service is still running and authenticated"
                    if acc.platform == "ctrader"
                    else "check the MT5 terminal/EA is still running and connected"
                )
                await send_alert(
                    f"**[{login}]** No report received in over {STALE_REPORT_SECONDS}s — {target}."
                )
            elif not stale and login in stale_alerted:
                stale_alerted.discard(login)
                await send_alert(f"**[{login}]** Reporting again — connection recovered.")


# ---------------- FastAPI endpoints (EA talks to these) ----------------

@app.post("/report")
async def report(request: Request):
    data = parse_form(await request.body())
    login = data.get("account", "")
    if not bridge_token_ok(data.get("token"), login):
        return PlainTextResponse("unauthorized", status_code=401)
    acc = bridge_account(login)
    if acc is None:
        return PlainTextResponse("unknown account", status_code=410)
    acc.last_report_ts = time.time()
    maybe_reset_day(acc)
    data["_received_at"] = str(time.time())
    acc.last_report = data
    acc.queue.extend(evaluate(acc.rule_config, acc.rule_state, data))

    # Self-heal: if our persisted count says this account should still be
    # blocked today but the EA's own state says it isn't (e.g. it just
    # restarted and lost its in-memory tradingBlocked flag), push BLOCK again.
    # Idempotent on the EA side — safe to resend even if it's already blocked.
    if acc.daily_loss_count >= MAX_LOSSES_PER_DAY and data.get("blocked") != "1":
        acc.queue.append("BLOCK")

    _record_equity_snapshot(acc, data)

    persist_account(acc)
    return PlainTextResponse("ok")


@app.get("/health")
async def health():
    """Liveness/readiness probe for uptime monitors (Kubernetes, systemd watchdog, etc.)."""
    return {"status": "ok"}


# ---------------- secure provisioning + Windows agent ----------------

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
}


def _setup_page(html_text: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(html_text, status_code=status_code, headers=_SECURITY_HEADERS)


def _agent_authenticated(request: Request) -> bool:
    return provisioning.agent_token_ok(request.headers.get("x-agent-token"))


def _notify(text: str):
    db.enqueue_notification(text)


def _instance_account(login: str):
    return accounts.get(login)


# --- Mobile API helpers ---

def _mobile_auth_or_401(request: Request):
    """Return auth token if valid, else raise 401."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    else:
        token = request.headers.get("X-Mobile-Token", "")
    if not MOBILE_API_TOKEN or not secrets.compare_digest(token, MOBILE_API_TOKEN):
        raise ValueError("Invalid token")
    return token


def _record_equity_snapshot(acc: AccountState, data: dict):
    """Store equity/balance on each report (throttled)."""
    now = time.time()
    last = getattr(acc, "_equity_ts", 0)
    if now - last < 60:
        return
    acc._equity_ts = now
    equity = float(data.get("equity", 0) or 0)
    balance = float(data.get("balance", 0) or 0)
    db.insert_equity_snapshot(acc.login, now, equity, balance)


def _set_instance_status(login: str, status: str, error: Optional[str] = None,
                         instance_dir: Optional[str] = None):
    acc = accounts.get(login)
    if acc:
        acc.instance_status = status
        if error is not None:
            acc.provision_error = error
        if instance_dir is not None:
            acc.instance_dir = instance_dir
        persist_account(acc)
    else:
        fields = {"instance_status": status}
        if error is not None:
            fields["provision_error"] = error
        if instance_dir is not None:
            fields["instance_dir"] = instance_dir
        db.update_account_provision(login, **fields)


@app.get("/provision/{token}")
async def provision_form(token: str):
    token_hash = provisioning.sha256_hex(token)
    row = db.get_provision_token(token_hash)
    if not row or row.get("used"):
        return _setup_page(provisioning.message_html(
            "Link no longer valid",
            "This setup link has already been used. Run <code>/addaccount</code> again in Discord to get a new one.",
            ok=False,
        ), 410)
    if float(row.get("expires_ts") or 0) < time.time():
        return _setup_page(provisioning.message_html(
            "Link expired",
            "This setup link expired. Run <code>/addaccount</code> again in Discord to get a new one.",
            ok=False,
        ), 410)
    acc = accounts.get(row["login"])
    if not acc or acc.phase is None:
        return _setup_page(provisioning.message_html(
            "Account not registered",
            "This account is no longer registered. Run <code>/addaccount</code> again.",
            ok=False,
        ), 410)
    return _setup_page(provisioning.form_response_html(acc.login, acc.server))


@app.post("/provision/{token}")
async def submit_provision_form(token: str, request: Request):
    token_hash = provisioning.sha256_hex(token)
    row = db.get_provision_token(token_hash)
    if not row or row.get("used"):
        return _setup_page(provisioning.message_html(
            "Link no longer valid",
            "This setup link has already been used. Run <code>/addaccount</code> again in Discord to get a new one.",
            ok=False,
        ), 410)
    if float(row.get("expires_ts") or 0) < time.time():
        return _setup_page(provisioning.message_html(
            "Link expired",
            "This setup link expired. Run <code>/addaccount</code> again in Discord to get a new one.",
            ok=False,
        ), 410)

    login = row["login"]
    acc = accounts.get(login)
    if not acc or acc.phase is None:
        return _setup_page(provisioning.message_html(
            "Account not registered",
            "This account is no longer registered. Run <code>/addaccount</code> again.",
            ok=False,
        ), 410)

    data = parse_form(await request.body())
    password = data.get("password", "")
    server = (data.get("server", "") or acc.server or "").strip()
    if not password or len(password) > 128:
        return _setup_page(provisioning.form_response_html(
            login, server, "Enter the MT5 trading password (1–128 characters)."
        ), 400)
    if not server or len(server) > 120:
        return _setup_page(provisioning.form_response_html(
            login, server, "Enter the exact MT5 server name."
        ), 400)

    bridge_token = provisioning.new_secret()
    try:
        payload_enc = provisioning.encrypt_json({
            "login": login,
            "server": server,
            "suffix": acc.suffix,
            "phase": acc.phase,
            "risk_pct": acc.risk_pct,
            "symbols": list(acc.symbols),
            "bridge_token": bridge_token,
            "password": password,
            "backend_url": provisioning.agent_backend_url(),
        })
    except provisioning.ProvisioningError as exc:
        return _setup_page(provisioning.message_html(
            "Backend not configured",
            f"Provisioning is unavailable: {exc}. Ask the administrator to set <code>CREDENTIAL_KEY</code>.",
            ok=False,
        ), 503)

    consumed = db.consume_provision_token(token_hash)
    if not consumed:
        return _setup_page(provisioning.message_html(
            "Link no longer valid",
            "This setup link was already used. Run <code>/addaccount</code> again.",
            ok=False,
        ), 410)

    acc.server = server
    acc.instance_status = "queued"
    acc.provision_error = ""
    acc.agent_id = "windows-1"
    acc.bridge_token_hash = provisioning.hash_bridge_token(bridge_token)
    persist_account(acc)
    db.create_provision_job(login, "provision", payload_enc)
    _notify(f"Password received for `{login}` — provisioning queued on the Windows agent.")
    return _setup_page(provisioning.message_html(
        "Details received",
        f"Account <strong>{login}</strong> is queued for provisioning. "
        "You can close this page; the bot will message you when the terminal is live.",
        ok=True,
    ))


@app.post("/agent/heartbeat")
async def agent_heartbeat(request: Request):
    if not _agent_authenticated(request):
        return PlainTextResponse("unauthorized", status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    agent_id = str(data.get("agent_id") or "windows-1")[:64]
    hostname = str(data.get("hostname") or "")[:128]
    instances = []
    for item in (data.get("instances") or [])[:500]:
        if not isinstance(item, dict):
            continue
        instances.append({
            "login": str(item.get("login") or "")[:64],
            "status": str(item.get("status") or "")[:24],
            "pid": int(item.get("pid") or 0),
            "dir": str(item.get("dir") or "")[:260],
        })
    stats = {
        "running": int(data.get("running") or 0),
        "total": int(data.get("total") or 0),
        "ram_free_mb": int(data.get("ram_free_mb") or 0),
        "ram_total_mb": int(data.get("ram_total_mb") or 0),
        "version": str(data.get("version") or "")[:32],
        "instances": instances,
    }
    db.upsert_agent_state(agent_id, hostname, json.dumps(stats), time.time())
    return {"ok": True}


@app.get("/agent/jobs")
async def agent_jobs(request: Request):
    if not _agent_authenticated(request):
        return PlainTextResponse("unauthorized", status_code=401)
    agent_id = str(request.headers.get("x-agent-id") or "windows-1")[:64]
    row = db.claim_next_job(agent_id)
    if not row:
        return {"job": None}
    payload = {}
    if row["payload_enc"]:
        try:
            payload = provisioning.decrypt_json(row["payload_enc"])
        except provisioning.ProvisioningError:
            db.complete_provision_job(row["id"], "failed", error="payload_decryption_failed")
            _set_instance_status(row["login"], "error", error="Could not decrypt provisioning payload")
            _notify(f"Provisioning failed for `{row['login']}`: backend could not decrypt the job payload.")
            return {"job": None, "error": "payload_decryption_failed"}
    if row["action"] == "provision":
        _set_instance_status(row["login"], "provisioning", error="", instance_dir=None)
    return {
        "job": {
            "id": row["id"],
            "action": row["action"],
            "login": row["login"],
            "payload": payload,
        }
    }


@app.post("/agent/jobs/{job_id}/result")
async def agent_job_result(job_id: int, request: Request):
    global default_account
    if not _agent_authenticated(request):
        return PlainTextResponse("unauthorized", status_code=401)
    job = db.get_provision_job(job_id)
    if not job:
        return PlainTextResponse("job not found", status_code=404)
    if job["status"] in ("done", "failed"):
        return PlainTextResponse("job already completed", status_code=409)
    try:
        data = await request.json()
    except Exception:
        data = {}
    status = str(data.get("status") or "failed").lower()
    if status not in ("done", "failed"):
        status = "failed"
    result = data.get("result") or ""
    error = str(data.get("error") or "")
    error = _redact_access_text(error)[:2000]
    result_text = json.dumps(result)[:4000] if not isinstance(result, str) else result[:4000]

    login = job["login"]
    action = job["action"]

    if action == "remove" and status == "done":
        db.complete_provision_job(job_id, status, result_text, error)
        accounts.pop(login, None)
        db.delete_account(login)
        if default_account == login:
            default_account = None
            db.set_setting("default_account", "")
        _notify(f"Account `{login}` removed and its MT5 instance stopped.")
        return {"ok": True}
    elif action == "provision":
        if status == "done":
            instance_dir = ""
            if isinstance(result, dict):
                instance_dir = str(result.get("instance_dir") or "")
            _set_instance_status(login, "running", error="", instance_dir=instance_dir or None)
            _notify(f"Account `{login}` is **live** — MT5 instance provisioned and the EA is running.")
        else:
            _set_instance_status(login, "error", error=error or "provisioning_failed")
            _notify(f"Provisioning failed for `{login}`: {error or 'unknown error'}. "
                    f"Check the password/server, then run `/provisionaccount` to get a fresh setup link.")
    elif action == "stop":
        if status == "done":
            _set_instance_status(login, "stopped", error="")
            _notify(f"MT5 instance for `{login}` stopped.")
        else:
            _set_instance_status(login, "error", error=error or "stop_failed")
            _notify(f"Could not stop the MT5 instance for `{login}`: {error or 'unknown error'}.")
    elif action == "restart":
        if status == "done":
            _set_instance_status(login, "running", error="")
            _notify(f"MT5 instance for `{login}` restarted.")
        else:
            _set_instance_status(login, "error", error=error or "restart_failed")
            _notify(f"Could not restart the MT5 instance for `{login}`: {error or 'unknown error'}.")
    db.complete_provision_job(job_id, status, result_text, error)
    return {"ok": True}


@app.get("/commands")
async def commands(token: str = "", account: str = ""):
    if not bridge_token_ok(token, account):
        return PlainTextResponse("unauthorized", status_code=401)
    acc = bridge_account(account)
    if acc is None:
        return PlainTextResponse("unknown account", status_code=410)
    pending, acc.queue = acc.queue, []
    # Always send the enabled symbol list and account limits so the EA knows
    # which quotes to report and can size/adopt manual pending orders with
    # the same hardcoded risk rules. The EA ignores unchanged symbols, so the
    # cost of repeating these on every poll is negligible.
    lines = [
        f"SYMBOLS {','.join(acc.symbols)}",
        f"RISK {acc.risk_pct or 0}",
        f"MAXPOS {MAX_OPEN_POSITIONS}",
        f"RRR {RISK_REWARD_RATIO}",
    ]
    lines.extend(pending)
    return PlainTextResponse("\n".join(lines))


REASON_LABELS = {"SL": "Hit Stop Loss", "TP": "Hit Take Profit",
                  "EA": "Closed by Bot", "MANUAL": "Closed Manually"}


@app.post("/event")
async def event(request: Request):
    data = parse_form(await request.body())
    login = data.get("account", "")
    if not bridge_token_ok(data.get("token"), login):
        return PlainTextResponse("unauthorized", status_code=401)
    message = data.get("message", "")
    acc = bridge_account(login)
    if acc is None:
        return PlainTextResponse("unknown account", status_code=410)
    maybe_reset_day(acc)

    parts = message.split()
    if parts and parts[0] == "TRADE_CLOSED" and len(parts) >= 6:
        _, symbol, ticket, reason, volume, profit = parts[:6]
        profit_val = float(profit)
        ts = time.time()

        # Backend-side dedup, defense-in-depth against the EA ever sending the
        # same close twice (e.g. mid-close restart) — the EA already aggregates
        # partial fills into one event, this just guards against a repeat of
        # that same event for a ticket we've already recorded moments ago.
        last_seen = acc.recent_closed_tickets.get(ticket)
        acc.recent_closed_tickets = {t: t_ts for t, t_ts in acc.recent_closed_tickets.items() if ts - t_ts < 300}
        if last_seen is not None and ts - last_seen < 30:
            return PlainTextResponse("duplicate ignored")
        acc.recent_closed_tickets[ticket] = ts

        acc.trade_log.append({
            "time": ts, "symbol": symbol, "ticket": ticket,
            "reason": reason, "volume": volume, "profit": profit_val,
        })
        db.insert_trade(login, ts, symbol, ticket, reason, volume, profit_val)

        # Send the actual close alert first — this describes what happened.
        label = REASON_LABELS.get(reason, reason)
        await send_alert(
            f"**[{login}]** Trade closed — {symbol}\n"
            f"{label} | P/L: {profit_val:.2f}\n"
            f"Ticket number: `#{ticket}`"
        )

        # Every losing trade counts, no matter who closed it (SL, TP-negative,
        # manual, or bot-forced) — only the alert is deduped so hitting the
        # limit doesn't spam a new "blocked" message on every loss after it.
        if profit_val < 0:
            acc.last_loss_time = ts
            acc.daily_loss_count += 1
            if acc.daily_loss_count >= MAX_LOSSES_PER_DAY and not acc.loss_limit_alerted:
                acc.loss_limit_alerted = True
                acc.queue.append("BLOCK")
                await send_alert(
                    f"**[{login}]** Hit {MAX_LOSSES_PER_DAY} losses for the day — "
                    f"trading blocked until tomorrow (Nairobi time)."
                )
        persist_account(acc)
        return PlainTextResponse("ok")

    # Every other event type (POSITION_OPENED, PENDING_TRIGGERED,
    # ORDER_REJECTED, etc.) is just forwarded as-is.
    await send_alert(f"**[{login}]** {message}")
    return PlainTextResponse("ok")


# ---------------- cTrader OAuth + connector API ----------------

_CTRADER_REFRESH_LOCKS: dict[int, asyncio.Lock] = {}
_CTRADER_ACCOUNT_TYPE = {0: "HEDGED", 1: "NETTED", 2: "SPREAD_BETTING"}


class GrantAuthError(RuntimeError):
    pass


class GrantTransientError(RuntimeError):
    pass


def _grant_lock(grant_id: int) -> asyncio.Lock:
    lock = _CTRADER_REFRESH_LOCKS.get(grant_id)
    if lock is None:
        lock = asyncio.Lock()
        _CTRADER_REFRESH_LOCKS[grant_id] = lock
    return lock


async def _grant_access_token(grant: dict) -> str:
    """Return a usable access token, refreshing and persisting rotation in place."""
    if grant.get("status") == "revoked":
        raise GrantAuthError("This cTrader connection has been revoked.")
    try:
        access = provisioning.decrypt_text(grant.get("access_token_enc") or "")
        refresh_token = provisioning.decrypt_text(grant.get("refresh_token_enc") or "")
    except Exception:
        raise GrantAuthError("Stored cTrader credentials could not be decrypted.") from None

    expires_at = float(grant.get("access_expires_at") or 0)
    if access and expires_at > time.time() + 3600:
        return access

    async with _grant_lock(int(grant["id"])):
        # Another request may have refreshed while we waited for the lock.
        fresh = db.get_ctrader_grant(int(grant["id"]))
        if not fresh:
            raise GrantAuthError("This cTrader connection no longer exists.")
        if fresh.get("status") == "revoked":
            raise GrantAuthError("This cTrader connection has been revoked.")
        if (fresh.get("access_token_enc") != grant.get("access_token_enc")
                and float(fresh.get("access_expires_at") or 0) > time.time() + 3600):
            return provisioning.decrypt_text(fresh["access_token_enc"])

        try:
            refresh_token = provisioning.decrypt_text(fresh.get("refresh_token_enc") or "")
            tokens = await asyncio.to_thread(ctrader_oauth.refresh, refresh_token)
        except ctrader_oauth.OAuthTransientError as exc:
            # A network blip must not force the owner through OAuth again.
            raise GrantTransientError(str(exc)[:500]) from None
        except Exception as exc:
            message = str(exc)[:500]
            db.update_ctrader_grant_status(int(grant["id"]), "reauth", message)
            db.enqueue_notification(
                f"**cTrader connection needs reauthorization.** Run `/addctrader` again.\n"
                f"Reason: {message}"
            )
            raise GrantAuthError(message) from None

        db.update_ctrader_grant_tokens(
            int(grant["id"]),
            provisioning.encrypt_text(tokens["access_token"]),
            provisioning.encrypt_text(tokens["refresh_token"]),
            time.time() + tokens["expires_in"],
        )
        return tokens["access_token"]


def _ctrader_page(html_text: str, status_code: int = 200) -> HTMLResponse:
    """Render a callback page and always clear the connect marker cookie."""
    response = _setup_page(html_text, status_code)
    response.delete_cookie(CTRADER_COOKIE, path="/ctrader")
    return response


@app.get("/ctrader/connect/{token}")
async def ctrader_connect(token: str):
    token_hash = provisioning.sha256_hex(token)
    row = db.get_ctrader_connect_token(token_hash)
    if not row or row["used"] or float(row["expires_ts"]) < time.time():
        return _setup_page(provisioning.message_html(
            "Link expired",
            "This cTrader link has already been used or has expired. Run "
            "<code>/addctrader</code> again in Discord to get a new one.",
            ok=False,
        ), 410)
    try:
        target = ctrader_oauth.authorization_url(token)
    except ctrader_oauth.OAuthError as exc:
        return _setup_page(provisioning.message_html(
            "cTrader is not configured",
            html.escape(str(exc)),
            ok=False,
        ), 503)
    response = RedirectResponse(target, status_code=302)
    # cTrader does not document an OAuth ``state`` parameter, so the callback
    # cannot rely on getting the one-time link back. Carry the (already
    # hashed) token in a short-lived HttpOnly cookie on our own origin; the
    # callback prefers an echoed ``state`` when cTrader provides one.
    response.set_cookie(
        CTRADER_COOKIE,
        token_hash,
        max_age=int(max(0.0, float(row["expires_ts"]) - time.time())),
        path="/ctrader",
        httponly=True,
        samesite="lax",
        secure=provisioning.PUBLIC_BASE_URL.startswith("https://"),
    )
    return response


def _usable_connect_row(row) -> bool:
    if not row or row.get("used"):
        return False
    return float(row.get("expires_ts") or 0) >= time.time()


@app.get("/ctrader/callback")
async def ctrader_callback(request: Request):
    error = request.query_params.get("error")
    state = request.query_params.get("state", "")
    candidates = []
    if state:
        candidates.append(db.get_ctrader_connect_token(provisioning.sha256_hex(state)))
    # cTrader omits ``state`` in its documented authorize URL. The marker
    # cookie set by /ctrader/connect identifies the link in that case.
    cookie_hash = request.cookies.get(CTRADER_COOKIE) or ""
    if cookie_hash:
        candidates.append(db.get_ctrader_connect_token(cookie_hash))
    row = next((item for item in candidates if _usable_connect_row(item)), None)
    if row is None:
        # Last resort: accept the only pending link. This is unambiguous and
        # lets a browser without the cookie complete the flow; two outstanding
        # links are refused instead of risking the wrong phase/risk settings.
        row = db.get_single_ctrader_connect_token()
    if row is None:
        return _ctrader_page(provisioning.message_html(
            "Link expired",
            "This cTrader link has already been used or has expired. Run "
            "<code>/addctrader</code> again in Discord to get a new one.",
            ok=False,
        ), 410)
    if error:
        db.consume_ctrader_connect_token(row["token_hash"])
        return _ctrader_page(provisioning.message_html(
            "cTrader authorization cancelled",
            f"cTrader returned <code>{html.escape(str(error))}</code>. Run "
            "<code>/addctrader</code> again when you are ready.",
            ok=False,
        ), 400)

    code = request.query_params.get("code", "")
    if not code:
        return _ctrader_page(provisioning.message_html(
            "No authorization code",
            "cTrader did not return an authorization code. Run "
            "<code>/addctrader</code> again.",
            ok=False,
        ), 400)

    try:
        tokens = await asyncio.to_thread(ctrader_oauth.exchange_code, code)
    except ctrader_oauth.OAuthError as exc:
        print(f"cTrader token exchange failed: {exc}")
        return _ctrader_page(provisioning.message_html(
            "cTrader connection failed",
            "The authorization code could not be exchanged for tokens. This is "
            "usually a redirect URI or client credential mismatch. Check the "
            "backend log and run <code>/addctrader</code> again.",
            ok=False,
        ), 502)

    # Consume only after a successful exchange so a bogus callback cannot burn
    # the owner's pending link.
    row = db.consume_ctrader_connect_token(row["token_hash"])
    if row is None:
        return _ctrader_page(provisioning.message_html(
            "Link expired",
            "This cTrader link was already used. Run <code>/addctrader</code> "
            "again in Discord to get a new one.",
            ok=False,
        ), 410)

    grant_id = db.create_ctrader_grant(
        discord_user=str(row.get("discord_user") or ""),
        phase=str(row.get("phase") or ""),
        risk_pct=float(row.get("risk_pct") or 0),
        environment="demo",
        access_token_enc=provisioning.encrypt_text(tokens["access_token"]),
        refresh_token_enc=provisioning.encrypt_text(tokens["refresh_token"]),
        access_expires_at=time.time() + tokens["expires_in"],
    )
    db.enqueue_notification(
        "**cTrader connected.** The connector is discovering your demo "
        "account(s). You'll get another message when they are ready."
    )
    return _ctrader_page(provisioning.message_html(
        "cTrader connected",
        "GuardRails now has an encrypted Open API grant. The VPS connector will "
        "discover your demo trading account(s) and enable them automatically. "
        "You can close this tab.",
        ok=True,
    ))


@app.get("/ctrader/grants")
async def ctrader_grants(request: Request):
    if not _connector_auth(request):
        return PlainTextResponse("unauthorized", status_code=401)
    grants = db.list_ctrader_grants(
        statuses=("pending", "active", "reauth", "error", "no_accounts", "no_demo_accounts")
    )
    return JSONResponse({
        "grants": [
            {
                "id": int(g["id"]),
                "status": g.get("status") or "pending",
                "environment": g.get("environment") or "demo",
                "error": g.get("error") or "",
                "created_ts": float(g.get("created_ts") or 0),
                "updated_ts": float(g.get("updated_ts") or 0),
            }
            for g in grants
        ]
    })


@app.get("/ctrader/grants/{grant_id}/session")
async def ctrader_grant_session(grant_id: int, request: Request):
    if not _connector_auth(request):
        return PlainTextResponse("unauthorized", status_code=401)
    grant = db.get_ctrader_grant(grant_id)
    if not grant:
        return JSONResponse({"error": "grant_not_found"}, status_code=404)
    try:
        access_token = await _grant_access_token(grant)
    except GrantAuthError as exc:
        return JSONResponse({"error": "reauth_required", "detail": str(exc)}, status_code=409)
    except GrantTransientError as exc:
        return JSONResponse(
            {"error": "temporarily_unavailable", "detail": str(exc)}, status_code=503
        )
    return JSONResponse({
        "grant_id": grant_id,
        "environment": grant.get("environment") or "demo",
        "access_token": access_token,
        "expires_at": float(db.get_ctrader_grant(grant_id).get("access_expires_at") or 0),
    })


@app.post("/ctrader/grants/{grant_id}/accounts")
async def ctrader_grant_accounts(grant_id: int, request: Request):
    if not _connector_auth(request):
        return PlainTextResponse("unauthorized", status_code=401)
    grant = db.get_ctrader_grant(grant_id)
    if not grant:
        return JSONResponse({"error": "grant_not_found"}, status_code=404)
    if grant.get("status") == "revoked":
        return JSONResponse({"error": "grant_revoked"}, status_code=410)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    items = body.get("accounts") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return JSONResponse({"error": "accounts_list_required"}, status_code=400)

    registered: list[str] = []
    skipped: list[dict] = []
    added: list[str] = []
    previous_grants: set[int] = set()
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("ctid_trader_account_id") or "").strip()
        if not raw_id.isdigit() or int(raw_id) <= 0:
            skipped.append({"ctid_trader_account_id": raw_id, "reason": "invalid_id"})
            continue
        # MVP boundary: never register live accounts. Demo testing must stay
        # impossible to confuse with a funded account.
        if bool(item.get("is_live")):
            skipped.append({"ctid_trader_account_id": raw_id, "reason": "live_not_supported"})
            continue

        external_id = raw_id
        login = f"ctrader:{external_id}"
        account_type = _CTRADER_ACCOUNT_TYPE.get(item.get("account_type"), "")
        display_name = str(item.get("display_name") or "").strip()
        if not display_name:
            broker = str(item.get("broker_name") or "").strip() or "cTrader"
            trader_login = str(item.get("trader_login") or "").strip()
            display_name = f"{broker} · {trader_login}" if trader_login else broker

        acc = accounts.get(login)
        is_new = acc is None
        reassigned = False
        if not is_new and acc.grant_id and int(acc.grant_id) != grant_id:
            if int(acc.grant_id) > grant_id:
                # A newer OAuth grant already owns this account. Do not let an
                # older grant that is still in discovery steal it back.
                skipped.append({
                    "ctid_trader_account_id": external_id,
                    "reason": "owned_by_newer_grant",
                })
                continue
            previous_grants.add(int(acc.grant_id))
            reassigned = True
        if is_new:
            acc = AccountState(login=login)
            acc.platform = "ctrader"
            acc.external_id = external_id
            acc.phase = grant.get("phase") or "challenge"
            acc.risk_pct = float(grant.get("risk_pct") or CHALLENGE_RISK_PCT)
            acc.suffix = "0"
            acc.instance_status = "external"
            acc.symbols = list(DEFAULT_SYMBOLS)
            accounts[login] = acc
        acc.platform = "ctrader"
        acc.external_id = external_id
        acc.display_name = display_name
        acc.account_environment = "demo"
        acc.account_type = account_type
        acc.grant_id = grant_id
        acc.server = str(item.get("broker_name") or "").strip()
        if is_new or reassigned:
            # A new OAuth grant is the reconnect path, so let /addctrader's
            # phase/risk apply to accounts that moved to it.
            acc.phase = grant.get("phase") or acc.phase or "challenge"
            acc.risk_pct = float(
                grant.get("risk_pct") or acc.risk_pct or CHALLENGE_RISK_PCT
            )
        if is_new:
            db.add_symbols(login, acc.symbols)
        persist_account(acc)
        registered.append(login)
        if is_new:
            added.append(login)
            db.enqueue_notification(
                f"**cTrader demo account connected:** `{login}` — {display_name}"
                f"{' (' + account_type + ')' if account_type else ''}\n"
                f"Phase: {acc.phase} · Risk: {acc.risk_pct}% · Symbols: {', '.join(acc.symbols)}"
                + ("\n⚠️ This is a NETTED account; manual-trade parity will be disabled."
                   if account_type == "NETTED" else "")
            )

    for old_id in previous_grants:
        # Reconnecting through /addctrader creates a new grant. Once its
        # accounts have moved, forget the superseded grant so its discovery
        # cannot compete for the same cTID.
        if db.revoke_ctrader_grant_if_empty(old_id):
            print(f"cTrader: revoked superseded grant {old_id}")

    current = db.get_ctrader_grant(grant_id)
    if current and current.get("status") == "revoked":
        # A racing request must not resurrect a grant the owner just removed.
        return JSONResponse({"registered": registered, "added": added, "skipped": skipped})
    if (not registered
            and any(s.get("reason") == "owned_by_newer_grant" for s in skipped)
            and not db.get_ctrader_accounts_for_grant(grant_id)):
        # This grant was superseded by a newer /addctrader connection and no
        # longer owns any account; stop its discovery loop for good.
        if db.revoke_ctrader_grant_if_empty(grant_id):
            print(f"cTrader: revoked superseded grant {grant_id}")
            return JSONResponse({"registered": registered, "added": added, "skipped": skipped})
    if registered:
        # Idempotent activation: a crash after registering accounts but before
        # flipping the grant left it ``pending`` and discovery retried every
        # tick forever. Any usable account means the connection is active.
        if not current or current.get("status") != "active":
            db.update_ctrader_grant_status(grant_id, "active", "")
    elif current and current.get("status") == "pending":
        if any(s["reason"] == "live_not_supported" for s in skipped):
            db.update_ctrader_grant_status(grant_id, "no_demo_accounts", "")
            db.enqueue_notification(
                "**cTrader connection has no demo accounts.** GuardRails only "
                "registers cTrader demo accounts in this build; the live account "
                "was left untouched."
            )
        elif not registered:
            db.update_ctrader_grant_status(grant_id, "no_accounts", "")
            db.enqueue_notification(
                "**cTrader connection returned no usable accounts.** Check that "
                "the cTID has a demo trading account and run `/addctrader` again."
            )
    return JSONResponse({"registered": registered, "added": added, "skipped": skipped})


@app.post("/ctrader/grants/{grant_id}/status")
async def ctrader_grant_status(grant_id: int, request: Request):
    if not _connector_auth(request):
        return PlainTextResponse("unauthorized", status_code=401)
    grant = db.get_ctrader_grant(grant_id)
    if not grant:
        return JSONResponse({"error": "grant_not_found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    status = str((body or {}).get("status") or "").strip()
    if status not in ("active", "error", "reauth", "no_accounts", "no_demo_accounts"):
        return JSONResponse({"error": "invalid_status"}, status_code=400)
    error = str((body or {}).get("error") or "")[:500]
    previous = grant.get("status")
    db.update_ctrader_grant_status(grant_id, status, error)
    if status in ("error", "reauth") and previous != status:
        db.enqueue_notification(
            f"**cTrader connector problem** (grant {grant_id}): {error or status}"
        )
    return JSONResponse({"ok": True})


@app.get("/ctrader/accounts")
async def ctrader_accounts(request: Request):
    if not _connector_auth(request):
        return PlainTextResponse("unauthorized", status_code=401)
    rows = db.list_ctrader_accounts()
    return JSONResponse({
        "accounts": [
            {
                "login": row["login"],
                "external_id": row.get("external_id") or "",
                "grant_id": int(row.get("grant_id") or 0),
                "environment": row.get("account_environment") or "demo",
                "account_type": row.get("account_type") or "",
                "display_name": row.get("display_name") or "",
                "phase": row.get("phase"),
                "risk_pct": row.get("risk_pct"),
                "symbols": db.load_symbols(row["login"]),
                "last_report_ts": float(row.get("last_report_ts") or 0),
            }
            for row in rows
        ]
    })


# ---------------- Mobile PWA API ----------------

def _mobile_auth(request: Request) -> bool:
    """Validate the mobile API token. Returns True if authorized, False otherwise."""
    auth = request.headers.get("Authorization", "")
    token = ""
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    elif "X-Mobile-Token" in request.headers:
        token = request.headers["X-Mobile-Token"].strip()
    if not MOBILE_API_TOKEN:
        return False
    return secrets.compare_digest(token, MOBILE_API_TOKEN)


@app.get("/api/mobile/accounts")
async def mobile_accounts(request: Request):
    if not _mobile_auth(request):
        raise HTTPException(401, "Unauthorized")
    acc_list: list[dict] = []
    for login, acc in accounts.items():
        if acc.phase is None:
            continue
        r = acc.last_report or {}
        acc_list.append({
            "login": login,
            "platform": acc.platform,
            "phase": acc.phase,
            "display_name": acc.display_name or login,
            "server": acc.server,
            "instance_status": acc.instance_status,
            "balance": float(r.get("balance", 0)),
            "equity": float(r.get("equity", 0)),
            "margin": float(r.get("margin", 0)),
            "free_margin": float(r.get("freemargin", 0)),
            "blocked": is_blocked(acc),
            "market_closed": bool(r.get("blocked") == "1"),
            "read_only": login in _MOBILE_READ_ONLY,
            "risk_pct": acc.risk_pct or 0,
            "max_positions": MAX_OPEN_POSITIONS,
            "symbols": acc.symbols,
            "last_report_ts": acc.last_report_ts,
        })
    return {
        "accounts": acc_list,
        "read_only_accounts": list(_MOBILE_READ_ONLY),
        "trade_accounts": list(_MOBILE_TRADE_ACCOUNTS),
    }


@app.get("/api/mobile/positions")
async def mobile_positions(request: Request):
    if not _mobile_auth(request):
        raise HTTPException(401, "Unauthorized")
    account = request.query_params.get("account", "")
    if account not in accounts:
        raise HTTPException(404, "Account not found")
    acc = accounts[account]
    r = acc.last_report or {}

    positions = []
    for p in parse_positions(r.get("positions", "")):
        positions.append({
            "ticket": p["ticket"],
            "symbol": p["symbol"],
            "type": p["type"],
            "volume": float(p["volume"]),
            "open_price": float(p.get("entry") or 0),
            "current": float(p.get("price") or 0),
            "profit": float(p["profit"]),
            "sl": float(p["sl"]) if p.get("sl") else None,
            "tp": float(p["tp"]) if p.get("tp") else None,
        })

    pending = []
    for p in parse_pending(r.get("pending", "")):
        pending.append({
            "ticket": p["ticket"],
            "symbol": p["symbol"],
            "type": p["type"],
            "price": float(p["price"]),
            "sl": float(p["sl"]) if p.get("sl") else None,
            "tp": float(p["tp"]) if p.get("tp") else None,
        })

    return {
        "account": account,
        "positions": positions,
        "pending": pending,
        "blocked": is_blocked(acc),
        "read_only": account in _MOBILE_READ_ONLY,
    }


# Mobile API: Candle data endpoint
@app.get("/api/mobile/candles")
async def mobile_candles(
    request: Request,
    symbol: str = "",
    timeframe: str = "M15",
):
    if not _mobile_auth(request):
        raise HTTPException(401, "Unauthorized")
    account = request.query_params.get("account", "")
    if account not in accounts:
        raise HTTPException(404, "Account not found")
    acc = accounts[account]
    r = acc.last_report or {}

    key = quote_key(symbol)
    data: list[dict] = []
    # Extract bid prices as candlesticks (simplified; real candles need history endpoint)
    bids = r.get(f"{key}_bid")
    asks = r.get(f"{key}_ask")
    ts = acc.last_report_ts
    if bids and asks and float(bids) > 0:
        bid = float(bids)
        ask = float(asks) if asks else bid
        mid = (bid + ask) / 2
        data.append({
            "time": int(ts),
            "open": mid,
            "high": mid * 1.001,
            "low": mid * 0.999,
            "close": mid,
            "volume": 1,
        })

    return {
        "account": account,
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": data,
    }


# Mobile API: Place or close trades
@app.post("/api/mobile/trade")
async def mobile_trade(request: Request):
    token = _mobile_auth(request)
    if token is None:
        raise HTTPException(401, "Unauthorized")
    data = await request.json()
    account = data.get("account", "")
    if account not in accounts:
        raise HTTPException(404, "Account not found")
    if account in _MOBILE_READ_ONLY:
        raise HTTPException(403, "This account is read-only (FTMO terminal)")

    acc = accounts[account]
    if is_blocked(acc):
        raise HTTPException(403, "Trading is blocked on this account")

    action = data.get("action", "open")
    if action == "close":
        ticket = data.get("ticket")
        if ticket:
            acc.queue.append(f"CLOSE {ticket}")
            return {"ok": True, "message": f"Close queued for ticket {ticket}"}
    if action == "cancel":
        ticket = data.get("ticket")
        if ticket:
            acc.queue.append(f"DELETE_PENDING {ticket}")
            return {"ok": True, "message": f"Cancel queued for ticket {ticket}"}
    if action == "open":
        side = data.get("side", "BUY").upper()
        symbol = data.get("symbol")
        if not symbol:
            raise HTTPException(400, "Symbol required")
        symbol_resolved, err = resolve_trade_symbol(acc, symbol)
        if err:
            raise HTTPException(400, err)
        risk_pct = float(data.get("risk_pct", acc.risk_pct or 1.0))
        lot_size = data.get("lot_size")
        sl = data.get("sl")
        tp = data.get("tp")
        kind = data.get("kind", "market")

        if kind == "market":
            tp_calc = compute_tp(side, 1.0, sl or 0.99) if sl else 0
            cmd = f"OPEN_MARKET {side} {symbol_resolved} {sl or 0} {risk_pct} {RISK_REWARD_RATIO}"
            acc.queue.append(cmd)
            return {"ok": True, "message": f"Market order queued for {side} {symbol_resolved}"}
        else:
            entry = sl or 1.0  # limit order uses sl as entry for simplicity
            tp_calc = compute_tp(side, entry, sl) if sl else entry + 0.01
            cmd = f"OPEN {side} {symbol_resolved} {entry} {sl} {tp_calc} {risk_pct}"
            acc.queue.append(cmd)
            return {"ok": True, "message": f"Limit order queued for {side} {symbol_resolved}"}

    raise HTTPException(400, "Unknown action")


@app.post("/api/mobile/trade/close")
async def mobile_trade_close(request: Request):
    return mobile_trade(request)


@app.post("/api/mobile/trade/cancel")
async def mobile_trade_cancel(request: Request):
    data = await request.json()
    if data.get("action") != "cancel":
        return await mobile_trade(request)
    account = data.get("account", "")
    if account not in accounts:
        raise HTTPException(404, "Account not found")
    ticket = data.get("ticket")
    if not ticket:
        raise HTTPException(400, "Ticket required")
    accounts[account].queue.append(f"DELETE_PENDING {ticket}")
    return {"ok": True, "message": f"Cancel queued for ticket {ticket}"}


async def send_alert(text: str) -> bool:
    if not ALERT_CHANNEL_ID:
        print(f"(no ALERT_CHANNEL_ID set) {text}")
        return False
    channel = client.get_channel(ALERT_CHANNEL_ID)
    if channel:
        try:
            await channel.send(text)
            return True
        except Exception as exc:  # Discord outage must not fail the request
            print(f"(alert send failed: {exc}) {text}")
    else:
        print(f"(alert channel not found) {text}")
    return False


async def notification_loop():
    """Deliver provisioning/job notifications created by HTTP endpoints."""
    await client.wait_until_ready()
    while True:
        try:
            pending = db.get_pending_notifications()
            for row in pending:
                delivered = await send_alert(row["text"])
                if not delivered and OWNER_ID:
                    try:
                        user = await client.fetch_user(OWNER_ID)
                        await user.send(row["text"])
                        delivered = True
                    except Exception as exc:
                        print(f"(notification DM failed: {exc})")
                if delivered:
                    db.mark_notification_sent(row["id"])
        except Exception as exc:
            print(f"(notification loop error: {exc})")
        await asyncio.sleep(5)


# ---------------- Discord bot ----------------

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID


async def deny(interaction: discord.Interaction):
    await interaction.response.send_message("Not authorized.", ephemeral=True)


@tree.command(name="addaccount", description="Register and provision a new MT5 account on the Windows host.")
@app_commands.describe(
    account="MT5 login number (this is the backend account key)",
    phase="Challenge or funded — sets the fixed risk %",
    server="Exact MT5 server name, e.g. FundingPips-Trial",
    suffix="Optional suffix appended to every symbol (0 for none; leave 0 when the suffix is already part of the MT5 symbol name)",
)
async def addaccount(interaction: discord.Interaction, account: str, phase: Literal["challenge", "funded"],
                     server: str, suffix: str = "0"):
    if not is_owner(interaction):
        return await deny(interaction)
    account = account.strip()
    if not account.isdigit():
        return await interaction.response.send_message("Account must be the numeric MT5 login.", ephemeral=True)
    server = server.strip()
    if not server or len(server) > 120:
        return await interaction.response.send_message("Server must be the exact MT5 server name (1–120 characters).", ephemeral=True)
    suffix = (suffix or "0").strip() or "0"
    if len(suffix) > 24:
        return await interaction.response.send_message("Suffix is too long (max 24 characters).", ephemeral=True)
    acc = accounts.get(account)
    if acc and acc.phase is not None:
        return await interaction.response.send_message(
            f"Account `{account}` is already registered as **{acc.phase}**. "
            f"Use `/provisionaccount` to retry provisioning, or `/removeaccount` first to change phase.",
            ephemeral=True,
        )
    acc = acc or get_or_create(account)
    acc.phase = phase
    acc.risk_pct = CHALLENGE_RISK_PCT if phase == "challenge" else FUNDED_RISK_PCT
    acc.server = server
    acc.suffix = suffix
    acc.instance_status = "awaiting_password"
    acc.provision_error = ""
    acc.agent_id = "windows-1"
    persist_account(acc)
    db.add_symbols(account, acc.symbols)

    global default_account
    note = ""
    if default_account is None:
        default_account = account
        db.set_setting("default_account", account)
        note = " It is now your default account."

    await interaction.response.defer(ephemeral=True)
    try:
        link = provisioning.create_provision_link(account)
    except provisioning.ProvisioningError as exc:
        return await interaction.followup.send(
            f"Registered `{account}` locally, but provisioning is not configured: {exc}",
            ephemeral=True,
        )

    agent_note = ""
    if not provisioning.agent_online():
        agent_note = ("\n\n⚠️ The Windows provisioning agent is **offline**, so the job will wait in the queue "
                      "until it reconnects.")

    message = (
        f"Account `{account}` registered as **{phase}** — risk fixed at **{acc.risk_pct}%**.\n"
        f"Server: `{acc.server}` · suffix: `{acc.suffix}`{note}\n\n"
        f"**One-time secure setup link** (expires in {provisioning.PROVISION_TOKEN_TTL // 60} min):\n"
        f"{link}\n\n"
        f"Open it, confirm the server, and enter the MT5 **trading password**. "
        f"The password is encrypted and is never sent to Discord.{agent_note}"
    )
    try:
        await interaction.user.send(message)
        await interaction.followup.send("Setup link sent to your DMs.", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(
            message + "\n\n(DMs are closed, so the link is shown here. It is single-use and expires shortly.)",
            ephemeral=True,
        )


@tree.command(name="addctrader", description="Connect a cTrader demo account through the official Open API.")
@app_commands.describe(phase="Challenge or funded — sets the fixed risk %")
async def addctrader(interaction: discord.Interaction, phase: Literal["challenge", "funded"]):
    if not is_owner(interaction):
        return await deny(interaction)
    if not ctrader_oauth.configured():
        return await interaction.response.send_message(
            "cTrader OAuth isn't configured on the backend yet. Set "
            "`CTRADER_CLIENT_ID`, `CTRADER_CLIENT_SECRET` and "
            "`CTRADER_REDIRECT_URI` in the VPS `.env`, then restart the backend.",
            ephemeral=True,
        )
    if not provisioning.PUBLIC_BASE_URL:
        return await interaction.response.send_message(
            "PUBLIC_BASE_URL isn't configured, so I can't build the cTrader link.",
            ephemeral=True,
        )

    risk_pct = CHALLENGE_RISK_PCT if phase == "challenge" else FUNDED_RISK_PCT
    raw = provisioning.new_secret()
    db.create_ctrader_connect_token(
        provisioning.sha256_hex(raw),
        str(interaction.user.id),
        phase,
        risk_pct,
        time.time() + provisioning.PROVISION_TOKEN_TTL,
    )
    link = f"{provisioning.PUBLIC_BASE_URL.rstrip('/')}/ctrader/connect/{raw}"
    message = (
        f"**cTrader demo connection** — phase **{phase}**, risk fixed at **{risk_pct}%**.\n"
        f"Open this single-use link within {provisioning.PROVISION_TOKEN_TTL // 60} minutes:\n"
        f"{link}\n\n"
        "Log in with your cTID and approve **trading** access. The connector will "
        "discover your demo account(s) automatically. Only demo accounts are "
        "registered in this build; live accounts are left untouched."
    )
    try:
        await interaction.user.send(message)
        await interaction.response.send_message("cTrader connect link sent to your DMs.", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        await interaction.response.send_message(message, ephemeral=True)


@tree.command(name="provisionaccount", description="Generate a fresh setup link for an account whose provisioning failed.")
async def provisionaccount(interaction: discord.Interaction, account: str):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"Account `{account}` isn't registered — use `/addaccount`.", ephemeral=True)
    if acc.platform == "ctrader":
        return await interaction.response.send_message(
            f"`{account}` is a cTrader account — it has no MT5 instance to provision. "
            f"Use `/removeaccount` and `/addctrader` if you need to reconnect it.",
            ephemeral=True,
        )
    if acc.instance_status == "running":
        return await interaction.response.send_message(
            f"`{account}` is already running. Use `/restartaccount` to restart it or `/removeaccount` to replace it.",
            ephemeral=True,
        )
    if not acc.server:
        return await interaction.response.send_message(
            f"`{account}` has no server on record. Remove it and add it again with `/addaccount`.",
            ephemeral=True,
        )

    await interaction.response.defer(ephemeral=True)
    try:
        link = provisioning.create_provision_link(account)
    except provisioning.ProvisioningError as exc:
        return await interaction.followup.send(f"Provisioning is not configured: {exc}", ephemeral=True)

    acc.instance_status = "awaiting_password"
    acc.provision_error = ""
    persist_account(acc)
    agent_note = "" if provisioning.agent_online() else "\n\n⚠️ The Windows agent is offline; the job will wait for it."
    message = (
        f"Fresh setup link for `{account}` (expires in {provisioning.PROVISION_TOKEN_TTL // 60} min):\n"
        f"{link}\n\nEnter the MT5 **trading password** there. It never appears in Discord.{agent_note}"
    )
    try:
        await interaction.user.send(message)
        await interaction.followup.send("Setup link sent to your DMs.", ephemeral=True)
    except (discord.Forbidden, discord.HTTPException):
        await interaction.followup.send(message, ephemeral=True)


def queue_account_job(login: str, action: str, extra: Optional[dict] = None) -> int:
    payload = {"login": login}
    if extra:
        payload.update(extra)
    return db.create_provision_job(login, action, provisioning.encrypt_json(payload))


@tree.command(name="removeaccount", description="Stop the MT5 instance and remove a registered account (locked during a losing period)")
async def removeaccount(interaction: discord.Interaction, account: str):
    global default_account
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"Account `{account}` isn't registered.", ephemeral=True)
    if is_in_losing_period(acc):
        unlock_time = removal_unlocks_at(acc)
        return await interaction.response.send_message(
            f"Can't remove `{account}` right now — it had a loss earlier today (Nairobi time). "
            f"This lock is intentional — it clears at the next daily reset, not affected by a "
            f"win in between.\nUnlocks at: **{unlock_time}**."
        )

    if acc.platform == "ctrader":
        grant_id = acc.grant_id
        open_positions = len(parse_positions(acc.last_report.get("positions", "")))
        pending_orders = len(parse_pending(acc.last_report.get("pending", "")))
        db.delete_account(account)
        accounts.pop(account, None)
        if default_account == account:
            default_account = None
            db.set_setting("default_account", "")
        revoked = db.revoke_ctrader_grant_if_empty(grant_id) if grant_id else False
        note = " The stored cTrader grant was revoked." if revoked else ""
        exposure = ""
        if open_positions or pending_orders:
            exposure = (
                f"\n\n⚠️ `{account}` still had **{open_positions} open position(s)** and "
                f"**{pending_orders} pending order(s)** on cTrader. They remain on the "
                f"broker and GuardRails will no longer manage them — close them in cTrader."
            )
        return await interaction.response.send_message(f"Removed `{account}`.{note}{exposure}")

    has_instance = bool(acc.instance_dir) or acc.instance_status in ("running", "stopped", "error", "provisioning", "queued")
    if not has_instance:
        db.delete_account(account)
        accounts.pop(account, None)
        if default_account == account:
            default_account = None
            db.set_setting("default_account", "")
        return await interaction.response.send_message(f"Removed `{account}` (no MT5 instance was running).")

    try:
        queue_account_job(account, "remove", {"instance_dir": acc.instance_dir})
    except provisioning.ProvisioningError as exc:
        return await interaction.response.send_message(f"Could not queue removal: {exc}", ephemeral=True)
    acc.instance_status = "removing"
    persist_account(acc)
    agent_note = "" if provisioning.agent_online() else "\n\n⚠️ The Windows agent is offline; removal will run when it reconnects."
    await interaction.response.send_message(
        f"Removal queued for `{account}`. The MT5 instance will be stopped and the account deleted.{agent_note}"
    )


@tree.command(name="stopaccount", description="Stop an account's MT5 instance without deleting the account.")
async def stopaccount(interaction: discord.Interaction, account: str):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"Account `{account}` isn't registered.", ephemeral=True)
    if acc.platform == "ctrader":
        return await interaction.response.send_message(
            f"`{account}` runs through the cTrader connector, not an MT5 instance.",
            ephemeral=True,
        )
    if not acc.instance_dir and acc.instance_status not in ("running", "provisioning", "queued"):
        return await interaction.response.send_message(f"`{account}` has no running instance to stop.", ephemeral=True)
    try:
        queue_account_job(account, "stop", {"instance_dir": acc.instance_dir})
    except provisioning.ProvisioningError as exc:
        return await interaction.response.send_message(f"Could not queue stop: {exc}", ephemeral=True)
    acc.instance_status = "stopping"
    persist_account(acc)
    await interaction.response.send_message(f"Stop queued for `{account}`.")


@tree.command(name="restartaccount", description="Restart an account's MT5 instance.")
async def restartaccount(interaction: discord.Interaction, account: str):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"Account `{account}` isn't registered.", ephemeral=True)
    if acc.platform == "ctrader":
        return await interaction.response.send_message(
            f"`{account}` is served by the cTrader connector and has no MT5 terminal to restart.",
            ephemeral=True,
        )
    if not acc.instance_dir:
        return await interaction.response.send_message(
            f"`{account}` has no provisioned instance yet. Use `/provisionaccount` first.", ephemeral=True
        )
    try:
        queue_account_job(account, "restart", {"instance_dir": acc.instance_dir})
    except provisioning.ProvisioningError as exc:
        return await interaction.response.send_message(f"Could not queue restart: {exc}", ephemeral=True)
    acc.instance_status = "restarting"
    persist_account(acc)
    await interaction.response.send_message(f"Restart queued for `{account}`.")


def human_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def live_instance(login: str) -> Optional[dict]:
    return provisioning.agent_instances().get(login)


def instance_label(acc: AccountState) -> str:
    if acc.platform == "ctrader":
        return "cTrader connector"
    live = live_instance(acc.login)
    if live:
        state = live.get("status") or "unknown"
        pid = live.get("pid")
        return f"{state}" + (f" (pid {pid})" if pid else "")
    return acc.instance_status or "unprovisioned"


def instance_detail(acc: AccountState) -> str:
    if acc.platform == "ctrader":
        bits = [
            "Connector: **cTrader Open API**",
            f"Broker: {acc.server or '—'}",
            f"Environment: {acc.account_environment or 'demo'}",
            f"Account type: {acc.account_type or '—'}",
        ]
        last = float(acc.last_report.get("_received_at", 0)) if acc.last_report else 0
        bits.append(
            f"last report: {human_age(time.time() - last)} ago" if last
            else "no connector report yet"
        )
        if acc.provision_error:
            bits.append(f"error: {acc.provision_error}")
        return " · ".join(bits)
    bits = [f"Instance: **{instance_label(acc)}**"]
    if acc.instance_dir:
        bits.append(f"dir: `{acc.instance_dir}`")
    if acc.provision_error:
        bits.append(f"error: {acc.provision_error}")
    last = float(acc.last_report.get("_received_at", 0)) if acc.last_report else 0
    if last:
        bits.append(f"last report: {human_age(time.time() - last)} ago")
    else:
        bits.append("no EA report yet")
    return " · ".join(bits)


@tree.command(name="accountstatus", description="Detailed status for one account, including its MT5 instance.")
async def accountstatus(interaction: discord.Interaction, account: str):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"Account `{account}` isn't registered.", ephemeral=True)
    lines = [
        f"**Account `{acc.login}`** — {acc.phase} ({acc.platform})",
        f"Risk per trade: {acc.risk_pct}%",
    ]
    if acc.platform == "ctrader":
        lines.append(f"Broker: `{acc.server or 'not set'}` · {acc.account_type or '—'} · {acc.account_environment or 'demo'}")
    else:
        lines.append(f"Server: `{acc.server or 'not set'}` · suffix `{acc.suffix}`")
    lines.append(instance_detail(acc))
    if acc.last_report:
        lines.append(f"Balance: {acc.last_report.get('balance', '—')} · Equity: {acc.last_report.get('equity', '—')}")
        lines.append(f"Open positions: {len(parse_positions(acc.last_report.get('positions', '')))}/{MAX_OPEN_POSITIONS}")
    if acc.platform != "ctrader":
        lines.append(provisioning.agent_summary())
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="setdefault", description="Set the account used by default when no account is specified")
async def setdefault(interaction: discord.Interaction, account: str):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{account}` isn't registered — use /addaccount first.")
    global default_account
    default_account = account
    db.set_setting("default_account", account)
    await interaction.response.send_message(f"Default account set to `{account}` ({acc.phase}).")


@tree.command(name="accounts", description="List all tracked accounts")
async def list_accounts(interaction: discord.Interaction):
    if not is_owner(interaction):
        return await deny(interaction)
    if not accounts:
        return await interaction.response.send_message("No accounts tracked yet.")
    lines = []
    for login, acc in accounts.items():
        phase = acc.phase or "unregistered"
        risk = f"{acc.risk_pct}%" if acc.risk_pct else "—"
        r = acc.last_report
        bal = r.get("balance", "—") if r else "—"
        eq = r.get("equity", "—") if r else "—"
        blocked = r.get("blocked", "—") if r else "—"
        locked = "Yes" if is_in_losing_period(acc) else "No"
        is_default = "Yes" if login == default_account else "No"
        open_count = len(parse_positions(r.get("positions", ""))) if r else 0
        lines.append(f"**Account** `{login}` ({acc.platform})")
        lines.append(f"Phase: {phase}")
        lines.append(f"Instance: {instance_label(acc)}")
        lines.append(f"Symbols: {', '.join(acc.symbols)}")
        lines.append(f"Risk per trade: {risk}")
        lines.append(f"Balance: {bal}")
        lines.append(f"Equity: {eq}")
        lines.append(f"Blocked: {blocked}")
        lines.append(f"Losses today: {acc.daily_loss_count}/{MAX_LOSSES_PER_DAY}")
        lines.append(f"Open positions: {open_count}/{MAX_OPEN_POSITIONS}")
        if acc.provision_error:
            lines.append(f"Provisioning error: {acc.provision_error}")
        if r:
            for symbol in acc.symbols:
                key = quote_key(symbol)
                lines.append(
                    f"{symbol} (ask/bid): {r.get(f'{key}_ask', '—')} / {r.get(f'{key}_bid', '—')}"
                )
        unlock_time = removal_unlocks_at(acc)
        lines.append(f"Removal locked: {locked}" + (f" (until {unlock_time})" if unlock_time else ""))
        lines.append(f"Default: {is_default}")
        lines.append("")
    lines.append(provisioning.agent_summary())
    await interaction.response.send_message("\n".join(lines).strip())


@tree.command(name="addsymbol", description="Enable exact MT5 symbols on an account (case matters)")
@app_commands.describe(symbols="Comma-separated MT5 symbols, e.g. EURUSD,US100.cash")
async def addsymbol(interaction: discord.Interaction, symbols: str, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered — use /addaccount first.")

    incoming, input_errors = parse_symbol_input(symbols)
    merged, added, already, merge_errors = merge_symbols(acc.symbols, incoming)
    if added:
        acc.symbols = merged
        db.add_symbols(acct, added)

    lines = []
    if added:
        lines.append("Added: " + ", ".join(f"`{s}`" for s in added))
    if already:
        lines.append("Already enabled: " + ", ".join(f"`{s}`" for s in already))
    problems = input_errors + merge_errors
    if problems:
        lines.append("Skipped: " + " ".join(problems))
    if not lines:
        lines.append("No symbols supplied.")
    lines.append("")
    lines.append(
        f"Enabled on `{acct}` ({len(acc.symbols)}/{MAX_SYMBOLS_PER_ACCOUNT}): "
        + ", ".join(acc.symbols)
    )
    lines.append("The EA picks the updated list up on its next poll.")
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="removesymbol", description="Disable a symbol (case-insensitive; the last symbol cannot be removed)")
async def removesymbol(interaction: discord.Interaction, symbol: str, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered — use /addaccount first.")

    value, error = validate_symbol(symbol)
    if error:
        return await interaction.response.send_message(error)
    stored = find_symbol(acc.symbols, value)
    if stored is None:
        return await interaction.response.send_message(
            f"`{value}` isn't enabled on `{acct}`. Enabled: {', '.join(acc.symbols)}."
        )
    if len(acc.symbols) <= 1:
        return await interaction.response.send_message(
            f"`{acct}` must keep at least one enabled symbol. Add another first, then remove this one."
        )
    if symbol_has_exposure(acc, stored):
        return await interaction.response.send_message(
            f"`{acct}` still has an open position or pending order on `{stored}`. "
            f"Close it first, then remove the symbol."
        )

    acc.symbols.remove(stored)
    db.remove_symbol(acct, stored)
    await interaction.response.send_message(
        f"Removed `{stored}` from `{acct}`. Enabled now: {', '.join(acc.symbols)}."
    )


@tree.command(name="symbols", description="List the symbols enabled on an account")
async def list_symbols(interaction: discord.Interaction, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered — use /addaccount first.")

    r = acc.last_report
    lines = [
        f"**Symbols on `{acct}`** ({len(acc.symbols)}/{MAX_SYMBOLS_PER_ACCOUNT})",
        "",
    ]
    for symbol in acc.symbols:
        key = quote_key(symbol)
        ask = r.get(key + "_ask") if r else None
        bid = r.get(key + "_bid") if r else None
        quote = f"{ask} / {bid}" if ask is not None and bid is not None else "no quote yet"
        lines.append(f"`{symbol}` — {quote}")
    lines.append("")
    lines.append("Add with `/addsymbol symbols:EURUSD,XAUUSD`; remove with `/removesymbol symbol:...`.")
    await interaction.response.send_message("\n".join(lines))


class CloseButton(discord.ui.Button):
    def __init__(self, account: str, ticket: str):
        super().__init__(label=f"Close #{ticket}", style=discord.ButtonStyle.danger)
        self.account = account
        self.ticket = ticket

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("Not authorized.", ephemeral=True)
        get_or_create(self.account).queue.append(f"CLOSE {self.ticket}")
        await interaction.response.send_message(f"Close queued for ticket #{self.ticket}.", ephemeral=True)


class CancelPendingButton(discord.ui.Button):
    def __init__(self, account: str, ticket: str):
        super().__init__(label=f"Cancel #{ticket}", style=discord.ButtonStyle.secondary)
        self.account = account
        self.ticket = ticket

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            return await interaction.response.send_message("Not authorized.", ephemeral=True)
        get_or_create(self.account).queue.append(f"DELETE_PENDING {self.ticket}")
        await interaction.response.send_message(f"Cancel queued for pending order #{self.ticket}.", ephemeral=True)


class StatusView(discord.ui.View):
    def __init__(self, account: str, positions: list, pending: list):
        super().__init__(timeout=None)
        for p in positions[:20]:
            self.add_item(CloseButton(account, p["ticket"]))
        for p in pending[:5]:
            self.add_item(CancelPendingButton(account, p["ticket"]))


@tree.command(name="status", description="Show account status and open positions (defaults to your default account)")
async def status(interaction: discord.Interaction, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc or not acc.last_report:
        return await interaction.response.send_message(f"No report received yet for `{acct}`.")
    r = acc.last_report

    positions = parse_positions(r.get("positions", ""))

    lines = [
        f"**Account** `{acct}` ({acc.phase or 'unregistered'} · {acc.platform})",
        "",
        f"Balance: {r.get('balance')}",
        f"Equity: {r.get('equity')}",
        f"Margin: {r.get('margin')}",
        f"Free margin: {r.get('freemargin')}",
        f"Blocked: {r.get('blocked')}",
        f"Losses today: {acc.daily_loss_count}/{MAX_LOSSES_PER_DAY}",
        f"Open positions: {len(positions)}/{MAX_OPEN_POSITIONS}",
    ]
    for symbol in acc.symbols:
        key = quote_key(symbol)
        lines.append(
            f"{symbol} (ask/bid): {r.get(f'{key}_ask', '—')} / {r.get(f'{key}_bid', '—')}"
        )

    lines.append("")
    lines.append(f"**Open positions ({len(positions)})**")
    if positions:
        for p in positions:
            lines.append("")
            lines.append(f"Ticket: `#{p['ticket']}`")
            lines.append(f"Type: {p['type']}")
            lines.append(f"Symbol: {p['symbol']}")
            lines.append(f"Volume: {p['volume']}")
            lines.append(f"P/L: {p['profit']}")
            if "sl" in p:
                lines.append(f"SL: {p['sl']}")
                lines.append(f"TP: {p['tp']}")
    else:
        lines.append("None")

    pending = parse_pending(r.get("pending", ""))
    lines.append("")
    lines.append(f"**Pending orders ({len(pending)})**")
    if pending:
        for p in pending:
            lines.append("")
            lines.append(f"Ticket: `#{p['ticket']}`")
            lines.append(f"Type: {p['type']}")
            lines.append(f"Symbol: {p['symbol']}")
            lines.append(f"Entry: {p['price']}")
            lines.append(f"SL: {p['sl']}")
            lines.append(f"TP: {p['tp']}")
    else:
        lines.append("None")

    view = StatusView(acct, positions, pending) if (positions or pending) else None
    await interaction.response.send_message("\n".join(lines), view=view)


@tree.command(name="pending", description="List pending limit/stop orders (defaults to your default account)")
async def pending(interaction: discord.Interaction, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc or not acc.last_report:
        return await interaction.response.send_message(f"No report received yet for `{acct}`.")
    orders = parse_pending(acc.last_report.get("pending", ""))
    if not orders:
        return await interaction.response.send_message(f"No pending orders on `{acct}`.")

    lines = [f"**Pending orders — `{acct}`**"]
    for p in orders:
        lines.append("")
        lines.append(f"Ticket: `#{p['ticket']}`")
        lines.append(f"Type: {p['type']}")
        lines.append(f"Symbol: {p['symbol']}")
        lines.append(f"Entry: {p['price']}")
        lines.append(f"SL: {p['sl']}")
        lines.append(f"TP: {p['tp']}")
    view = StatusView(acct, [], orders)
    await interaction.response.send_message("\n".join(lines), view=view)


@tree.command(name="cancelpending", description="Cancel a pending limit/stop order by ticket")
async def cancelpending(interaction: discord.Interaction, ticket: int, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    get_or_create(acct).queue.append(f"DELETE_PENDING {ticket}")
    await interaction.response.send_message(f"Cancel queued for pending order #{ticket} on `{acct}`.")


@tree.command(name="closeall", description="Close every bot-opened position (defaults to your default account)")
async def closeall(interaction: discord.Interaction, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    get_or_create(acct).queue.append("CLOSE_ALL")
    await interaction.response.send_message(f"Close-all queued for `{acct}`.")


@tree.command(name="close", description="Close one position by ticket number (defaults to your default account)")
async def close(interaction: discord.Interaction, ticket: int, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    get_or_create(acct).queue.append(f"CLOSE {ticket}")
    await interaction.response.send_message(f"Close queued for ticket {ticket} on `{acct}`.")


@tree.command(name="buy", description=f"Buy: symbol, entry and SL. TP auto-calculated at {RISK_REWARD_RATIO} RRR.")
async def buy(interaction: discord.Interaction, symbol: str, entry: float, sl: float, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered — use /addaccount first.")
    symbol, symbol_error = resolve_trade_symbol(acc, symbol)
    if symbol_error:
        return await interaction.response.send_message(symbol_error)
    if is_blocked(acc):
        return await interaction.response.send_message(
            f"Trading is blocked on `{acct}`. No manual override — it stays blocked "
            f"until the daily reset (midnight Nairobi time)."
        )
    if is_at_position_limit(acc):
        return await interaction.response.send_message(
            f"Already at the max of {MAX_OPEN_POSITIONS} open positions on `{acct}` — "
            f"close one first (`/close` or `/closeall`)."
        )
    if is_market_closed_for(acc, symbol):
        return await interaction.response.send_message(
            f"Market for {symbol} appears closed on `{acct}` (no live quotes) — not queued. "
            f"Try again when it's open."
        )
    tp = compute_tp("BUY", entry, sl)
    cmd = f"OPEN BUY {symbol} {entry} {sl} {tp} {acc.risk_pct}"
    acc.queue.append(cmd)
    await interaction.response.send_message(
        "**BUY queued**\n\n"
        f"Account: {acct} ({acc.phase})\n"
        f"Symbol: {symbol}\n"
        f"Entry: {entry}\n"
        f"SL: {sl}\n"
        f"TP: {tp:.5f} (auto, {RISK_REWARD_RATIO} RRR)\n"
        f"Risk: {acc.risk_pct}%"
    )


@tree.command(name="sell", description=f"Sell: symbol, entry and SL. TP auto-calculated at {RISK_REWARD_RATIO} RRR.")
async def sell(interaction: discord.Interaction, symbol: str, entry: float, sl: float, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered — use /addaccount first.")
    symbol, symbol_error = resolve_trade_symbol(acc, symbol)
    if symbol_error:
        return await interaction.response.send_message(symbol_error)
    if is_blocked(acc):
        return await interaction.response.send_message(
            f"Trading is blocked on `{acct}`. No manual override — it stays blocked "
            f"until the daily reset (midnight Nairobi time)."
        )
    if is_at_position_limit(acc):
        return await interaction.response.send_message(
            f"Already at the max of {MAX_OPEN_POSITIONS} open positions on `{acct}` — "
            f"close one first (`/close` or `/closeall`)."
        )
    if is_market_closed_for(acc, symbol):
        return await interaction.response.send_message(
            f"Market for {symbol} appears closed on `{acct}` (no live quotes) — not queued. "
            f"Try again when it's open."
        )
    tp = compute_tp("SELL", entry, sl)
    cmd = f"OPEN SELL {symbol} {entry} {sl} {tp} {acc.risk_pct}"
    acc.queue.append(cmd)
    await interaction.response.send_message(
        "**SELL queued**\n\n"
        f"Account: {acct} ({acc.phase})\n"
        f"Symbol: {symbol}\n"
        f"Entry: {entry}\n"
        f"SL: {sl}\n"
        f"TP: {tp:.5f} (auto, {RISK_REWARD_RATIO} RRR)\n"
        f"Risk: {acc.risk_pct}%"
    )


@tree.command(name="marketbuy", description=f"Market buy: symbol and SL. Entry fetched live by the EA. TP at {RISK_REWARD_RATIO} RRR.")
async def marketbuy(interaction: discord.Interaction, symbol: str, sl: float, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered — use /addaccount first.")
    symbol, symbol_error = resolve_trade_symbol(acc, symbol)
    if symbol_error:
        return await interaction.response.send_message(symbol_error)
    if is_blocked(acc):
        return await interaction.response.send_message(
            f"Trading is blocked on `{acct}`. No manual override — it stays blocked "
            f"until the daily reset (midnight Nairobi time)."
        )
    if is_at_position_limit(acc):
        return await interaction.response.send_message(
            f"Already at the max of {MAX_OPEN_POSITIONS} open positions on `{acct}` — "
            f"close one first (`/close` or `/closeall`)."
        )
    if is_market_closed_for(acc, symbol):
        return await interaction.response.send_message(
            f"Market for {symbol} appears closed on `{acct}` (no live quotes) — not queued. "
            f"Try again when it's open."
        )
    cmd = f"OPEN_MARKET BUY {symbol} {sl} {acc.risk_pct} {RISK_REWARD_RATIO}"
    acc.queue.append(cmd)
    r = acc.last_report
    last_known = r.get(quote_key(symbol) + "_ask") if r else None
    await interaction.response.send_message(
        "**MARKET BUY queued**\n\n"
        f"Account: {acct} ({acc.phase})\n"
        f"Symbol: {symbol}\n"
        f"Entry: fetched live by the EA at execution"
        + (f" (last known ask: {last_known})" if last_known else "") + "\n"
        f"SL: {sl}\n"
        f"TP: calculated at execution ({RISK_REWARD_RATIO} RRR)\n"
        f"Risk: {acc.risk_pct}%"
    )


@tree.command(name="marketsell", description=f"Market sell: symbol and SL. Entry fetched live by the EA. TP at {RISK_REWARD_RATIO} RRR.")
async def marketsell(interaction: discord.Interaction, symbol: str, sl: float, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered — use /addaccount first.")
    symbol, symbol_error = resolve_trade_symbol(acc, symbol)
    if symbol_error:
        return await interaction.response.send_message(symbol_error)
    if is_blocked(acc):
        return await interaction.response.send_message(
            f"Trading is blocked on `{acct}`. No manual override — it stays blocked "
            f"until the daily reset (midnight Nairobi time)."
        )
    if is_at_position_limit(acc):
        return await interaction.response.send_message(
            f"Already at the max of {MAX_OPEN_POSITIONS} open positions on `{acct}` — "
            f"close one first (`/close` or `/closeall`)."
        )
    if is_market_closed_for(acc, symbol):
        return await interaction.response.send_message(
            f"Market for {symbol} appears closed on `{acct}` (no live quotes) — not queued. "
            f"Try again when it's open."
        )
    cmd = f"OPEN_MARKET SELL {symbol} {sl} {acc.risk_pct} {RISK_REWARD_RATIO}"
    acc.queue.append(cmd)
    r = acc.last_report
    last_known = r.get(quote_key(symbol) + "_bid") if r else None
    await interaction.response.send_message(
        "**MARKET SELL queued**\n\n"
        f"Account: {acct} ({acc.phase})\n"
        f"Symbol: {symbol}\n"
        f"Entry: fetched live by the EA at execution"
        + (f" (last known bid: {last_known})" if last_known else "") + "\n"
        f"SL: {sl}\n"
        f"TP: calculated at execution ({RISK_REWARD_RATIO} RRR)\n"
        f"Risk: {acc.risk_pct}%"
    )


@tree.command(name="weekly", description="Quick 7-day trade summary (defaults to your default account)")
async def weekly(interaction: discord.Interaction, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set — use /setdefault.")
    acc = accounts.get(acct)
    if not acc:
        return await interaction.response.send_message(f"`{acct}` isn't tracked.")

    cutoff = time.time() - 7 * 24 * 3600
    trades = [t for t in acc.trade_log if t["time"] >= cutoff]
    if not trades:
        return await interaction.response.send_message(f"No closed trades in the last 7 days for `{acct}`.")

    wins = [t for t in trades if t["profit"] > 0]
    total_profit = sum(t["profit"] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    sl_hits = sum(1 for t in trades if t["reason"] == "SL")
    tp_hits = sum(1 for t in trades if t["reason"] == "TP")
    manual = len(trades) - sl_hits - tp_hits
    best = max(trades, key=lambda t: t["profit"])
    worst = min(trades, key=lambda t: t["profit"])

    msg = (
        f"**7-day summary — `{acct}`** ({acc.phase or 'unregistered'})\n\n"
        f"Trades: {len(trades)}\n"
        f"Win rate: {win_rate:.0f}%\n"
        f"Net P/L: {total_profit:.2f}\n\n"
        f"Closed by TP: {tp_hits}\n"
        f"Closed by SL: {sl_hits}\n"
        f"Closed Manually/Other: {manual}\n\n"
        f"Best trade: {best['symbol']}({best['profit']:.2f})\n"
        f"Ticket number: `#{best['ticket']}`\n\n"
        f"Worst trade: {worst['symbol']}({worst['profit']:.2f})\n"
        f"Ticked number: `#{worst['ticket']}`"
    )
    await interaction.response.send_message(msg)


@client.event
async def on_ready():
    symbols_now = sorted({symbol for acc in accounts.values() for symbol in acc.symbols})
    label = ",".join(symbols_now) if symbols_now else "none"
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        tree.clear_commands(guild=None)
        await tree.sync()
        await tree.sync(guild=guild)
        print(f"Discord bot logged in as {client.user} — symbols {label} (synced to guild {GUILD_ID}, global cleared)")
    else:
        await tree.sync()
        print(f"Discord bot logged in as {client.user} — symbols {label} (global sync — may take up to an hour to appear)")
    await send_alert(f"Bot online. Tracking {len(accounts)} account(s). Default: {default_account or 'none set'}.")


async def main():
    install_access_log_redaction()
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(
        server.serve(),
        client.start(DISCORD_TOKEN),
        watchdog_loop(),
        notification_loop(),
    )


if __name__ == "__main__":
    db.init_db()
    load_state()
    asyncio.run(main())

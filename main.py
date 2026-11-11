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
  SIGNAL_TOKEN         optional — separate token for POST /signal; falls back to BRIDGE_TOKEN
  SIGNAL_DEDUPE_SECONDS optional, default 300 — duplicate webhook window
  DISCORD_TOKEN         required
  OWNER_DISCORD_ID      required
  ALERT_CHANNEL_ID      required
  GUILD_ID              optional — instant command sync while testing
  DAILY_LOSS_LIMIT / MAX_DRAWDOWN_PCT / MAX_LOT_SIZE / MAX_POSITIONS   optional, default 0 (disabled)
  DB_PATH               optional, default bot_data.db
"""

import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timezone, timedelta
import asyncio
from dataclasses import dataclass, field
from typing import Literal, Optional
from urllib.parse import parse_qs

from dotenv import load_dotenv
load_dotenv()

import discord
from discord import app_commands
from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi import FastAPI
import uvicorn

from rules import RuleConfig, RuleState, evaluate
from symbols import (
    DEFAULT_SYMBOLS,
    MAX_SYMBOLS_PER_ACCOUNT,
    merge_symbols,
    parse_symbol_input,
    quote_key,
    validate_symbol,
)
import signals
import storage as db


def _token_list(value: str) -> tuple[str, ...]:
    """BRIDGE_TOKEN may list several tokens (comma/space separated) so a
    token can be rotated without breaking an EA that is still using the old
    one. Put the new token first; remove the old one once the EA is updated."""
    return tuple(token for token in re.split(r"[,;\s]+", value or "") if token)


_BRIDGE_TOKENS = _token_list(os.environ.get("BRIDGE_TOKEN", "changeme"))
TOKEN = _BRIDGE_TOKENS[0]  # primary token, used as the /signal fallback
SIGNAL_TOKEN = os.environ.get("SIGNAL_TOKEN") or TOKEN


def bridge_token_ok(supplied) -> bool:
    if not supplied:
        return False
    candidate = str(supplied).encode("utf-8")
    return any(
        secrets.compare_digest(candidate, token.encode("utf-8"))
        for token in _BRIDGE_TOKENS
    )


try:
    SIGNAL_DEDUPE_SECONDS = max(30, int(os.environ.get("SIGNAL_DEDUPE_SECONDS", "300") or "300"))
except ValueError:
    SIGNAL_DEDUPE_SECONDS = 300
MAX_SIGNAL_BODY_BYTES = 64 * 1024
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_DISCORD_ID", "0"))
ALERT_CHANNEL_ID = int(os.environ.get("ALERT_CHANNEL_ID", "0") or "0")
GUILD_ID = os.environ.get("GUILD_ID")

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
    r"(?i)((?:signal[_-]?|bridge[_-]?)?token|secret|api[_-]?key)=([^&\s\"']+)"
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


@dataclass
class AccountState:
    login: str
    phase: Optional[str] = None
    risk_pct: Optional[float] = None
    suffix: str = "0"
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
    """Validate a Discord symbol against the account's enabled list."""
    symbol, error = validate_symbol(raw)
    if error:
        return None, error
    if symbol not in acc.symbols:
        return None, (
            f"`{symbol}` isn't enabled on `{acc.login}`. "
            f"Add it with `/addsymbol symbols:{symbol}`."
        )
    return symbol, None


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
    )


def load_state():
    """Called once at startup — repopulates accounts, trade_log, and default_account from disk."""
    global default_account
    for row in db.load_accounts():
        acc = AccountState(login=row["login"])
        acc.phase = row["phase"]
        acc.risk_pct = row["risk_pct"]
        acc.suffix = row["suffix"] or "0"
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
        accounts[login] = AccountState(login=login)
    return accounts[login]


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
                await send_alert(
                    f"**[{login}]** No report received in over {STALE_REPORT_SECONDS}s — "
                    f"check the MT5 terminal/EA is still running and connected."
                )
            elif not stale and login in stale_alerted:
                stale_alerted.discard(login)
                await send_alert(f"**[{login}]** Reporting again — connection recovered.")


# ---------------- FastAPI endpoints (EA talks to these) ----------------

@app.post("/report")
async def report(request: Request):
    data = parse_form(await request.body())
    if not bridge_token_ok(data.get("token")):
        return PlainTextResponse("unauthorized", status_code=401)
    login = data.get("account", "")
    acc = get_or_create(login)
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

    persist_account(acc)
    return PlainTextResponse("ok")


@app.get("/health")
async def health():
    """Liveness/readiness probe for uptime monitors (Kubernetes, systemd watchdog, etc.)."""
    return {"status": "ok"}


@app.get("/commands")
async def commands(token: str = "", account: str = ""):
    if not bridge_token_ok(token):
        return PlainTextResponse("unauthorized", status_code=401)
    acc = get_or_create(account)
    pending, acc.queue = acc.queue, []
    # Always send the enabled symbol list so the EA knows which quotes to
    # report. The EA ignores this line when the list hasn't changed, so the
    # cost of repeating it on every poll is negligible.
    lines = [f"SYMBOLS {','.join(acc.symbols)}"]
    lines.extend(pending)
    return PlainTextResponse("\n".join(lines))


REASON_LABELS = {"SL": "Hit Stop Loss", "TP": "Hit Take Profit",
                  "EA": "Closed by Bot", "MANUAL": "Closed Manually"}


@app.post("/event")
async def event(request: Request):
    data = parse_form(await request.body())
    if not bridge_token_ok(data.get("token")):
        return PlainTextResponse("unauthorized", status_code=401)
    login = data.get("account", "")
    message = data.get("message", "")
    acc = get_or_create(login)
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


@app.post("/signal")
async def receive_signal(request: Request):
    """Accept a trading signal from any alerting platform.

    Authentication accepts the shared secret as ``?token=``, an
    ``X-Signal-Token`` header, a bearer token, a JSON field, or a
    ``TOKEN=...`` fragment inside the alert message — whichever the
    platform can actually send. Everything beyond extraction is enforced
    by the same guardrails as the Discord commands.
    """
    try:
        declared_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared_length = 0
    if declared_length > MAX_SIGNAL_BODY_BYTES:
        return JSONResponse({"ok": False, "error": "payload_too_large"}, status_code=413)

    raw_body = await request.body()
    if len(raw_body) > MAX_SIGNAL_BODY_BYTES:
        return JSONResponse({"ok": False, "error": "payload_too_large"}, status_code=413)

    payload = signals.decode_body(raw_body, request.headers.get("content-type", ""))
    supplied = signals.extract_token(
        payload, query=request.query_params, headers=request.headers
    )
    if not supplied or not secrets.compare_digest(
        str(supplied).encode("utf-8"), SIGNAL_TOKEN.encode("utf-8")
    ):
        print(f"[signal] unauthorized request from {request.client}")
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    # The query parameter wins, then an account in the payload, then the
    # Discord-configured default account.
    account = (request.query_params.get("account") or "").strip()
    parsed = signals.parse_signal(payload)
    account = account or (parsed.account or "") or (default_account or "")
    acc = accounts.get(account) if account else None
    if acc is not None:
        # Re-parse with this account's enabled symbols so an index name in
        # free text (US30, NAS100, ...) is recognised even without a slash.
        parsed = signals.parse_signal(payload, known_symbols=acc.symbols)

    event_key = parsed.event_key(account)
    redacted_message = signals.redact_text(parsed.message, [SIGNAL_TOKEN])
    raw_for_db = json.dumps(
        signals.redact_payload(payload, [SIGNAL_TOKEN]),
        ensure_ascii=True,
        default=str,
    )[:8000]

    # Safe setup mode: parse and report, but never claim or queue. This is
    # how to inspect an undocumented OpenMarket payload without risking a
    # live order: POST ...&dry_run=1
    dry_run = (request.query_params.get("dry_run") or "").lower() in {"1", "true", "yes"}
    if dry_run:
        print(
            f"[signal] dry-run account={account or '?'} side={parsed.side or '?'} "
            f"symbol={parsed.symbol or '?'} sl={parsed.sl if parsed.sl is not None else '?'} "
            f"raw={raw_for_db}"
        )
        return JSONResponse({
            "ok": True,
            "status": "dry_run",
            "account": account or None,
            "symbol": parsed.symbol,
            "side": parsed.side,
            "sl": parsed.sl,
            "entry": parsed.entry,
            "message": redacted_message,
            "missing": parsed.missing,
            "conflicts": parsed.conflicts,
        })

    force = (request.query_params.get("force") or "").lower() in {"1", "true", "yes"}
    claim_window = 0 if force else SIGNAL_DEDUPE_SECONDS
    if not db.claim_signal(
        event_key,
        time.time(),
        account or None,
        parsed.symbol,
        parsed.side,
        parsed.sl,
        redacted_message,
        raw_for_db,
        claim_window,
    ):
        # Platform retry — return 200 so it stops retrying, but do nothing.
        return JSONResponse({"ok": True, "status": "duplicate", "event": event_key[:12]})

    print(
        f"[signal] captured account={account or '?'} side={parsed.side or '?'} "
        f"symbol={parsed.symbol or '?'} sl={parsed.sl if parsed.sl is not None else '?'} "
        f"raw={raw_for_db}"
    )

    # Webhook setup buttons often send an empty body or a "test" message.
    # Acknowledge those with 200 so the destination saves, but never queue.
    if not (parsed.side or parsed.symbol or parsed.sl is not None or parsed.message):
        db.update_signal(event_key, "ignored", "empty payload")
        return JSONResponse({"ok": True, "status": "ignored"})
    if parsed.message.strip().lower() in {"test", "ping", "test alert", "this is a test"}:
        db.update_signal(event_key, "ignored", "test payload")
        return JSONResponse({"ok": True, "status": "test"})

    async def reject(reason: str, status_code: int = 422):
        db.update_signal(event_key, "rejected", reason)
        details = f"{parsed.side or '?'} {parsed.symbol or '?'}"
        lines = [
            f"**[{account or 'unknown'}] Signal rejected — {details}**",
            f"Reason: {reason}",
        ]
        if parsed.sl is not None:
            lines.append(f"SL: {format_price(parsed.sl)}")
        if redacted_message:
            lines.append(f"Alert: {redacted_message[:300]}")
        await send_alert("\n".join(lines))
        body = {
            "ok": False,
            "status": "rejected",
            "error": reason,
            "event": event_key[:12],
        }
        if parsed.missing:
            body["missing"] = parsed.missing
        return JSONResponse(body, status_code=status_code)

    if not account:
        return await reject("No account in the payload/query and no default account set.", 400)
    if acc is None or acc.phase is None:
        return await reject(f"Account `{account}` is not registered — use /addaccount first.", 404)
    if parsed.conflicts:
        return await reject(
            "Conflicting signal fields: " + "; ".join(parsed.conflicts), 422
        )
    if parsed.missing:
        return await reject(
            "Missing required field(s): "
            + ", ".join(parsed.missing)
            + ". Send e.g. `BUY EURUSD SL=1.0800`.",
            422,
        )

    symbol, symbol_error = resolve_trade_symbol(acc, parsed.symbol)
    if symbol_error:
        return await reject(symbol_error, 422)

    if is_blocked(acc):
        return await reject(
            f"Trading is blocked on `{account}` after {MAX_LOSSES_PER_DAY} losses "
            "today — resets at Nairobi midnight.",
            409,
        )
    if is_at_position_limit(acc):
        return await reject(
            f"Already at the max of {MAX_OPEN_POSITIONS} open positions on `{account}`.",
            409,
        )
    if is_market_closed_for(acc, symbol):
        return await reject(f"Market for {symbol} appears closed (no live quotes).", 409)

    # Reject an SL on the wrong side of the current quote. If no quote has
    # arrived yet, fail open and let the EA/broker make the final call.
    report = acc.last_report
    if report:
        key = quote_key(symbol)
        try:
            if parsed.side == "BUY":
                reference = float(report.get(f"{key}_ask") or 0)
                if reference > 0 and parsed.sl >= reference:
                    return await reject(
                        f"BUY SL {format_price(parsed.sl)} is not below the current ask "
                        f"{format_price(reference)}.",
                        409,
                    )
            else:
                reference = float(report.get(f"{key}_bid") or 0)
                if reference > 0 and parsed.sl <= reference:
                    return await reject(
                        f"SELL SL {format_price(parsed.sl)} is not above the current bid "
                        f"{format_price(reference)}.",
                        409,
                    )
        except (TypeError, ValueError):
            pass

    sl_text = format_price(parsed.sl)
    acc.queue.append(
        f"OPEN_MARKET {parsed.side} {symbol} {sl_text} {acc.risk_pct} {RISK_REWARD_RATIO}"
    )
    db.update_signal(event_key, "queued")

    source = f" via {parsed.platform}" if parsed.platform else ""
    lines = [
        f"**[{account}] Signal queued — {parsed.side} {symbol}**",
        f"Source: webhook{source}",
        f"SL: {sl_text}",
        f"Risk: {acc.risk_pct}% | TP: {RISK_REWARD_RATIO}R at execution",
    ]
    if parsed.entry is not None:
        lines.append(f"Alert price: {format_price(parsed.entry)}")
    if redacted_message:
        lines.append(f"Alert: {redacted_message[:300]}")
    lines.append(f"Event: `{event_key[:12]}`")
    await send_alert("\n".join(lines))

    return JSONResponse({
        "ok": True,
        "status": "queued",
        "event": event_key[:12],
        "account": account,
        "symbol": symbol,
        "side": parsed.side,
        "sl": parsed.sl,
    })


@app.get("/signals")
async def signal_history(token: str = "", limit: int = 20, include_raw: bool = False):
    """Recent webhook captures — handy for mapping an undocumented payload."""
    if not secrets.compare_digest(
        token.encode("utf-8"), SIGNAL_TOKEN.encode("utf-8")
    ):
        return PlainTextResponse("unauthorized", status_code=401)
    rows = db.recent_signals(limit)
    if not include_raw:
        for row in rows:
            row.pop("raw", None)
    return {"signals": rows}


async def send_alert(text: str):
    if not ALERT_CHANNEL_ID:
        print(f"(no ALERT_CHANNEL_ID set) {text}")
        return
    channel = client.get_channel(ALERT_CHANNEL_ID)
    if channel:
        try:
            await channel.send(text)
        except Exception as exc:  # Discord outage must not fail a webhook
            print(f"(alert send failed: {exc}) {text}")
    else:
        print(f"(alert channel not found) {text}")


# ---------------- Discord bot ----------------

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID


async def deny(interaction: discord.Interaction):
    await interaction.response.send_message("Not authorized.", ephemeral=True)


@tree.command(name="addaccount", description="Register an MT5 account. Risk % is fixed by phase, not adjustable.")
async def addaccount(interaction: discord.Interaction, account: str, phase: Literal["challenge", "funded"]):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = get_or_create(account)
    if acc.phase is not None:
        return await interaction.response.send_message(
            f"Account `{account}` is already registered as **{acc.phase}**. Remove it first to change phase."
        )
    acc.phase = phase
    acc.risk_pct = CHALLENGE_RISK_PCT if phase == "challenge" else FUNDED_RISK_PCT
    persist_account(acc)
    db.add_symbols(account, acc.symbols)

    global default_account
    note = ""
    if default_account is None:
        default_account = account
        db.set_setting("default_account", account)
        note = " (set as your default account, since none was set)"
    await interaction.response.send_message(
        f"Registered `{account}` as **{phase}** — risk per trade fixed at **{acc.risk_pct}%**. "
        f"Symbols enabled: {', '.join(acc.symbols)} (change with `/addsymbol`). "
        f"The MT5 EA must have the right `SymbolSuffix` set for your broker.{note}"
    )



@tree.command(name="removeaccount", description="Remove a registered account (locked during a losing period)")
async def removeaccount(interaction: discord.Interaction, account: str):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"Account `{account}` isn't registered.")
    if is_in_losing_period(acc):
        unlock_time = removal_unlocks_at(acc)
        return await interaction.response.send_message(
            f"Can't remove `{account}` right now — it had a loss earlier today (Nairobi time). "
            f"This lock is intentional — it clears at the next daily reset, not affected by a "
            f"win in between.\nUnlocks at: **{unlock_time}**."
        )
    del accounts[account]
    db.delete_account(account)
    global default_account
    if default_account == account:
        default_account = None
        db.set_setting("default_account", "")
    await interaction.response.send_message(f"Removed `{account}`.")


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
        lines.append(f"**Account** `{login}`")
        lines.append(f"Phase: {phase}")
        lines.append(f"Symbols: {', '.join(acc.symbols)}")
        lines.append(f"Risk per trade: {risk}")
        lines.append(f"Balance: {bal}")
        lines.append(f"Equity: {eq}")
        lines.append(f"Blocked: {blocked}")
        lines.append(f"Losses today: {acc.daily_loss_count}/{MAX_LOSSES_PER_DAY}")
        lines.append(f"Open positions: {open_count}/{MAX_OPEN_POSITIONS}")
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
    await interaction.response.send_message("\n".join(lines).strip())


@tree.command(name="addsymbol", description="Enable one or more symbols on an account (bare broker roots, no suffix)")
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


@tree.command(name="removesymbol", description="Disable a symbol on an account (the last symbol cannot be removed)")
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
    if value not in acc.symbols:
        return await interaction.response.send_message(
            f"`{value}` isn't enabled on `{acct}`. Enabled: {', '.join(acc.symbols)}."
        )
    if len(acc.symbols) <= 1:
        return await interaction.response.send_message(
            f"`{acct}` must keep at least one enabled symbol. Add another first, then remove this one."
        )
    if symbol_has_exposure(acc, value):
        return await interaction.response.send_message(
            f"`{acct}` still has an open position or pending order on `{value}`. "
            f"Close it first, then remove the symbol."
        )

    acc.symbols.remove(value)
    db.remove_symbol(acct, value)
    await interaction.response.send_message(
        f"Removed `{value}` from `{acct}`. Enabled now: {', '.join(acc.symbols)}."
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
        f"**Account** `{acct}` ({acc.phase or 'unregistered'})",
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
    )


if __name__ == "__main__":
    db.init_db()
    load_state()
    asyncio.run(main())

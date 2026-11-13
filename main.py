"""
Backend for the Discord-controlled MT5 bot â€” multi-account, persistent version.

Runs two things concurrently:
  - A FastAPI server the EA(s) talk to (POST /report, GET /commands, POST /event)
  - A Discord bot you talk to (slash commands)

State (accounts, rule progress, trade history, default account) is persisted
to a local SQLite file (storage.py) so a backend restart doesn't lose it.

DEFAULT ACCOUNT: use /setdefault once â€” after that, account is optional on
every other command and falls back to it.

HARDCODED â€” not changeable via any Discord command, by design:
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
  GUILD_ID              optional â€” instant command sync while testing
  PUBLIC_BASE_URL       public HTTPS URL used for one-time setup links
  CREDENTIAL_KEY        Fernet key used to encrypt provisioning payloads
  AGENT_TOKEN           shared secret for the Windows provisioning agent
  DAILY_LOSS_LIMIT / MAX_DRAWDOWN_PCT / MAX_LOT_SIZE / MAX_POSITIONS   optional, default 0 (disabled)
  DB_PATH               optional, default bot_data.db
"""

import asyncio
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
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi import FastAPI
import uvicorn

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


def bridge_token_ok(supplied, account: str = "") -> bool:
    if not supplied:
        return False
    candidate = str(supplied)
    if account:
        expected_hash = db.get_bridge_token_hash(account)
        if expected_hash and provisioning.bridge_token_matches(candidate, expected_hash):
            return True
    candidate_bytes = candidate.encode("utf-8")
    return any(
        secrets.compare_digest(candidate_bytes, token.encode("utf-8"))
        for token in _BRIDGE_TOKENS
    )


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


NAIROBI_OFFSET = timedelta(hours=3)  # EAT is fixed UTC+3, no DST â€” safe to hardcode


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
    server: str = ""
    instance_status: str = "unprovisioned"
    instance_dir: str = ""
    provision_error: str = ""
    agent_id: str = ""
    bridge_token_hash: str = ""
    last_report_ts: float = 0.0
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
    # Authoritative on our own persisted count first â€” the EA's self-reported
    # "blocked" flag is just its in-memory state, which resets to false on any
    # EA restart (recompile, terminal restart) with no way for it to know on
    # its own that today's limit was already hit before it came back up.
    if acc.daily_loss_count >= MAX_LOSSES_PER_DAY:
        return True
    return bool(acc.last_report) and acc.last_report.get("blocked") == "1"


def is_market_closed_for(acc: "AccountState", symbol: str) -> bool:
    """True only when we have explicit zero-quote data for this symbol â€” fails
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
    )


def load_state():
    """Called once at startup â€” repopulates accounts, trade_log, and default_account from disk."""
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
        # tradingBlocked flag doesn't know the day rolled over â€” nothing else
        # tells it to. Without this, the EA stays blocked forever after its
        # first block, since BLOCK was the only command ever sent for it.
        acc.queue.append("UNBLOCK")
        persist_account(acc)


def is_in_losing_period(acc: AccountState) -> bool:
    """Locked only for the rest of the Nairobi calendar day the last loss happened
    on â€” clears automatically at the next Nairobi midnight, same boundary as the
    daily loss-count/rule resets. Not a rolling 24h timer."""
    if not acc.last_loss_time:
        return False
    loss_date = (datetime.fromtimestamp(acc.last_loss_time, tz=timezone.utc) + NAIROBI_OFFSET).date().isoformat()
    return loss_date == today_str()


def removal_unlocks_at(acc: AccountState) -> Optional[str]:
    """Human-readable UTC timestamp for the next Nairobi midnight after the last
    loss â€” when the removal lock clears â€” or None if not currently locked."""
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
    """Warns if an account's EA stops reporting â€” closed terminal, dropped connection, etc."""
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
                    f"**[{login}]** No report received in over {STALE_REPORT_SECONDS}s â€” "
                    f"check the MT5 terminal/EA is still running and connected."
                )
            elif not stale and login in stale_alerted:
                stale_alerted.discard(login)
                await send_alert(f"**[{login}]** Reporting again â€” connection recovered.")


# ---------------- FastAPI endpoints (EA talks to these) ----------------

@app.post("/report")
async def report(request: Request):
    data = parse_form(await request.body())
    login = data.get("account", "")
    if not bridge_token_ok(data.get("token"), login):
        return PlainTextResponse("unauthorized", status_code=401)
    acc = get_or_create(login)
    acc.last_report_ts = time.time()
    maybe_reset_day(acc)
    data["_received_at"] = str(time.time())
    acc.last_report = data
    acc.queue.extend(evaluate(acc.rule_config, acc.rule_state, data))

    # Self-heal: if our persisted count says this account should still be
    # blocked today but the EA's own state says it isn't (e.g. it just
    # restarted and lost its in-memory tradingBlocked flag), push BLOCK again.
    # Idempotent on the EA side â€” safe to resend even if it's already blocked.
    if acc.daily_loss_count >= MAX_LOSSES_PER_DAY and data.get("blocked") != "1":
        acc.queue.append("BLOCK")

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
            login, server, "Enter the MT5 trading password (1â€“128 characters)."
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
            "backend_url": provisioning.PUBLIC_BASE_URL,
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
    _notify(f"Password received for `{login}` â€” provisioning queued on the Windows agent.")
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
            _notify(f"Account `{login}` is **live** â€” MT5 instance provisioned and the EA is running.")
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
    acc = get_or_create(account)
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
    acc = get_or_create(login)
    maybe_reset_day(acc)

    parts = message.split()
    if parts and parts[0] == "TRADE_CLOSED" and len(parts) >= 6:
        _, symbol, ticket, reason, volume, profit = parts[:6]
        profit_val = float(profit)
        ts = time.time()

        # Backend-side dedup, defense-in-depth against the EA ever sending the
        # same close twice (e.g. mid-close restart) â€” the EA already aggregates
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

        # Send the actual close alert first â€” this describes what happened.
        label = REASON_LABELS.get(reason, reason)
        await send_alert(
            f"**[{login}]** Trade closed â€” {symbol}\n"
            f"{label} | P/L: {profit_val:.2f}\n"
            f"Ticket number: `#{ticket}`"
        )

        # Every losing trade counts, no matter who closed it (SL, TP-negative,
        # manual, or bot-forced) â€” only the alert is deduped so hitting the
        # limit doesn't spam a new "blocked" message on every loss after it.
        if profit_val < 0:
            acc.last_loss_time = ts
            acc.daily_loss_count += 1
            if acc.daily_loss_count >= MAX_LOSSES_PER_DAY and not acc.loss_limit_alerted:
                acc.loss_limit_alerted = True
                acc.queue.append("BLOCK")
                await send_alert(
                    f"**[{login}]** Hit {MAX_LOSSES_PER_DAY} losses for the day â€” "
                    f"trading blocked until tomorrow (Nairobi time)."
                )
        persist_account(acc)
        return PlainTextResponse("ok")

    # Every other event type (POSITION_OPENED, PENDING_TRIGGERED,
    # ORDER_REJECTED, etc.) is just forwarded as-is.
    await send_alert(f"**[{login}]** {message}")
    return PlainTextResponse("ok")


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
    phase="Challenge or funded â€” sets the fixed risk %",
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
        return await interaction.response.send_message("Server must be the exact MT5 server name (1â€“120 characters).", ephemeral=True)
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
        agent_note = ("\n\nâš ï¸ The Windows provisioning agent is **offline**, so the job will wait in the queue "
                      "until it reconnects.")

    message = (
        f"Account `{account}` registered as **{phase}** â€” risk fixed at **{acc.risk_pct}%**.\n"
        f"Server: `{acc.server}` Â· suffix: `{acc.suffix}`{note}\n\n"
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


@tree.command(name="provisionaccount", description="Generate a fresh setup link for an account whose provisioning failed.")
async def provisionaccount(interaction: discord.Interaction, account: str):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"Account `{account}` isn't registered â€” use `/addaccount`.", ephemeral=True)
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
    agent_note = "" if provisioning.agent_online() else "\n\nâš ï¸ The Windows agent is offline; the job will wait for it."
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
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"Account `{account}` isn't registered.", ephemeral=True)
    if is_in_losing_period(acc):
        unlock_time = removal_unlocks_at(acc)
        return await interaction.response.send_message(
            f"Can't remove `{account}` right now â€” it had a loss earlier today (Nairobi time). "
            f"This lock is intentional â€” it clears at the next daily reset, not affected by a "
            f"win in between.\nUnlocks at: **{unlock_time}**."
        )

    has_instance = bool(acc.instance_dir) or acc.instance_status in ("running", "stopped", "error", "provisioning", "queued")
    if not has_instance:
        db.delete_account(account)
        accounts.pop(account, None)
        global default_account
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
    agent_note = "" if provisioning.agent_online() else "\n\nâš ï¸ The Windows agent is offline; removal will run when it reconnects."
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
    live = live_instance(acc.login)
    if live:
        state = live.get("status") or "unknown"
        pid = live.get("pid")
        return f"{state}" + (f" (pid {pid})" if pid else "")
    return acc.instance_status or "unprovisioned"


def instance_detail(acc: AccountState) -> str:
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
    return " Â· ".join(bits)


@tree.command(name="accountstatus", description="Detailed status for one account, including its MT5 instance.")
async def accountstatus(interaction: discord.Interaction, account: str):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"Account `{account}` isn't registered.", ephemeral=True)
    lines = [
        f"**Account `{acc.login}`** â€” {acc.phase}",
        f"Risk per trade: {acc.risk_pct}%",
        f"Server: `{acc.server or 'not set'}` Â· suffix `{acc.suffix}`",
        instance_detail(acc),
    ]
    if acc.last_report:
        lines.append(f"Balance: {acc.last_report.get('balance', 'â€”')} Â· Equity: {acc.last_report.get('equity', 'â€”')}")
        lines.append(f"Open positions: {len(parse_positions(acc.last_report.get('positions', '')))}/{MAX_OPEN_POSITIONS}")
    lines.append(provisioning.agent_summary())
    await interaction.response.send_message("\n".join(lines))


@tree.command(name="setdefault", description="Set the account used by default when no account is specified")
async def setdefault(interaction: discord.Interaction, account: str):
    if not is_owner(interaction):
        return await deny(interaction)
    acc = accounts.get(account)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{account}` isn't registered â€” use /addaccount first.")
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
        risk = f"{acc.risk_pct}%" if acc.risk_pct else "â€”"
        r = acc.last_report
        bal = r.get("balance", "â€”") if r else "â€”"
        eq = r.get("equity", "â€”") if r else "â€”"
        blocked = r.get("blocked", "â€”") if r else "â€”"
        locked = "Yes" if is_in_losing_period(acc) else "No"
        is_default = "Yes" if login == default_account else "No"
        open_count = len(parse_positions(r.get("positions", ""))) if r else 0
        lines.append(f"**Account** `{login}`")
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
                    f"{symbol} (ask/bid): {r.get(f'{key}_ask', 'â€”')} / {r.get(f'{key}_bid', 'â€”')}"
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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered â€” use /addaccount first.")

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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered â€” use /addaccount first.")

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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered â€” use /addaccount first.")

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
        lines.append(f"`{symbol}` â€” {quote}")
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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
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
            f"{symbol} (ask/bid): {r.get(f'{key}_ask', 'â€”')} / {r.get(f'{key}_bid', 'â€”')}"
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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    acc = accounts.get(acct)
    if not acc or not acc.last_report:
        return await interaction.response.send_message(f"No report received yet for `{acct}`.")
    orders = parse_pending(acc.last_report.get("pending", ""))
    if not orders:
        return await interaction.response.send_message(f"No pending orders on `{acct}`.")

    lines = [f"**Pending orders â€” `{acct}`**"]
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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    get_or_create(acct).queue.append(f"DELETE_PENDING {ticket}")
    await interaction.response.send_message(f"Cancel queued for pending order #{ticket} on `{acct}`.")


@tree.command(name="closeall", description="Close every bot-opened position (defaults to your default account)")
async def closeall(interaction: discord.Interaction, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    get_or_create(acct).queue.append("CLOSE_ALL")
    await interaction.response.send_message(f"Close-all queued for `{acct}`.")


@tree.command(name="close", description="Close one position by ticket number (defaults to your default account)")
async def close(interaction: discord.Interaction, ticket: int, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    get_or_create(acct).queue.append(f"CLOSE {ticket}")
    await interaction.response.send_message(f"Close queued for ticket {ticket} on `{acct}`.")


@tree.command(name="buy", description=f"Buy: symbol, entry and SL. TP auto-calculated at {RISK_REWARD_RATIO} RRR.")
async def buy(interaction: discord.Interaction, symbol: str, entry: float, sl: float, account: Optional[str] = None):
    if not is_owner(interaction):
        return await deny(interaction)
    acct = resolve_account(account)
    if not acct:
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered â€” use /addaccount first.")
    symbol, symbol_error = resolve_trade_symbol(acc, symbol)
    if symbol_error:
        return await interaction.response.send_message(symbol_error)
    if is_blocked(acc):
        return await interaction.response.send_message(
            f"Trading is blocked on `{acct}`. No manual override â€” it stays blocked "
            f"until the daily reset (midnight Nairobi time)."
        )
    if is_at_position_limit(acc):
        return await interaction.response.send_message(
            f"Already at the max of {MAX_OPEN_POSITIONS} open positions on `{acct}` â€” "
            f"close one first (`/close` or `/closeall`)."
        )
    if is_market_closed_for(acc, symbol):
        return await interaction.response.send_message(
            f"Market for {symbol} appears closed on `{acct}` (no live quotes) â€” not queued. "
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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered â€” use /addaccount first.")
    symbol, symbol_error = resolve_trade_symbol(acc, symbol)
    if symbol_error:
        return await interaction.response.send_message(symbol_error)
    if is_blocked(acc):
        return await interaction.response.send_message(
            f"Trading is blocked on `{acct}`. No manual override â€” it stays blocked "
            f"until the daily reset (midnight Nairobi time)."
        )
    if is_at_position_limit(acc):
        return await interaction.response.send_message(
            f"Already at the max of {MAX_OPEN_POSITIONS} open positions on `{acct}` â€” "
            f"close one first (`/close` or `/closeall`)."
        )
    if is_market_closed_for(acc, symbol):
        return await interaction.response.send_message(
            f"Market for {symbol} appears closed on `{acct}` (no live quotes) â€” not queued. "
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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered â€” use /addaccount first.")
    symbol, symbol_error = resolve_trade_symbol(acc, symbol)
    if symbol_error:
        return await interaction.response.send_message(symbol_error)
    if is_blocked(acc):
        return await interaction.response.send_message(
            f"Trading is blocked on `{acct}`. No manual override â€” it stays blocked "
            f"until the daily reset (midnight Nairobi time)."
        )
    if is_at_position_limit(acc):
        return await interaction.response.send_message(
            f"Already at the max of {MAX_OPEN_POSITIONS} open positions on `{acct}` â€” "
            f"close one first (`/close` or `/closeall`)."
        )
    if is_market_closed_for(acc, symbol):
        return await interaction.response.send_message(
            f"Market for {symbol} appears closed on `{acct}` (no live quotes) â€” not queued. "
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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
    acc = accounts.get(acct)
    if not acc or acc.phase is None:
        return await interaction.response.send_message(f"`{acct}` isn't registered â€” use /addaccount first.")
    symbol, symbol_error = resolve_trade_symbol(acc, symbol)
    if symbol_error:
        return await interaction.response.send_message(symbol_error)
    if is_blocked(acc):
        return await interaction.response.send_message(
            f"Trading is blocked on `{acct}`. No manual override â€” it stays blocked "
            f"until the daily reset (midnight Nairobi time)."
        )
    if is_at_position_limit(acc):
        return await interaction.response.send_message(
            f"Already at the max of {MAX_OPEN_POSITIONS} open positions on `{acct}` â€” "
            f"close one first (`/close` or `/closeall`)."
        )
    if is_market_closed_for(acc, symbol):
        return await interaction.response.send_message(
            f"Market for {symbol} appears closed on `{acct}` (no live quotes) â€” not queued. "
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
        return await interaction.response.send_message("No account specified and no default set â€” use /setdefault.")
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
        f"**7-day summary â€” `{acct}`** ({acc.phase or 'unregistered'})\n\n"
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
        print(f"Discord bot logged in as {client.user} â€” symbols {label} (synced to guild {GUILD_ID}, global cleared)")
    else:
        await tree.sync()
        print(f"Discord bot logged in as {client.user} â€” symbols {label} (global sync â€” may take up to an hour to appear)")
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

"""
SQLite persistence for the bot backend. No separate database server needed —
this is a single file (bot_data.db by default) written next to main.py.

Everything here is a thin write-through layer: main.py keeps its in-memory
dicts for speed during normal operation, and calls these functions to
persist changes so a backend restart doesn't lose state.
"""

import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "bot_data.db")


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                login TEXT PRIMARY KEY,
                phase TEXT,
                risk_pct REAL,
                suffix TEXT DEFAULT '0',
                day_start_balance REAL DEFAULT 0,
                peak_equity REAL DEFAULT 0,
                already_blocked INTEGER DEFAULT 0,
                last_loss_time REAL,
                last_reset_date TEXT,
                daily_loss_count INTEGER DEFAULT 0
            )
        """)
        # migration for databases created before the suffix column existed
        try:
            conn.execute("ALTER TABLE accounts ADD COLUMN suffix TEXT DEFAULT '0'")
        except sqlite3.OperationalError:
            pass  # column already exists
        # provisioning/instance columns (added later; safe to run repeatedly)
        for column, ddl in (
            ("server", "TEXT DEFAULT ''"),
            ("instance_status", "TEXT DEFAULT 'unprovisioned'"),
            ("instance_dir", "TEXT DEFAULT ''"),
            ("provision_error", "TEXT DEFAULT ''"),
            ("bridge_token_hash", "TEXT DEFAULT ''"),
            ("last_report_ts", "REAL DEFAULT 0"),
            # cTrader accounts share the same rules/storage as MT5 accounts.
            # `login` stays the internal account key (e.g. "ctrader:439...");
            # the platform-specific identity lives in the columns below.
            ("platform", "TEXT DEFAULT 'mt5'"),
            ("external_id", "TEXT DEFAULT ''"),
            ("display_name", "TEXT DEFAULT ''"),
            ("account_environment", "TEXT DEFAULT ''"),
            ("account_type", "TEXT DEFAULT ''"),
            ("grant_id", "INTEGER DEFAULT 0"),
        ):
            try:
                conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:
                pass
        # Per-account symbol whitelist. The backend stores bare broker roots
        # (EURUSD, XAUUSD, ...); each EA appends its own configured suffix.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_symbols (
                login TEXT NOT NULL,
                symbol TEXT NOT NULL,
                PRIMARY KEY (login, symbol)
            )
        """)
        # One-shot MT5 provisioning jobs consumed on the VPS by
        # tools/linux_mt5_provision.py. payload_enc holds the encrypted JSON
        # payload and is cleared as soon as the provisioner reports a result.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provision_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_enc TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                created_ts REAL DEFAULT 0,
                claimed_ts REAL DEFAULT 0,
                completed_ts REAL DEFAULT 0,
                result TEXT DEFAULT '',
                error TEXT DEFAULT ''
            )
        """)
        # One-time HTTPS setup links for /addaccount. Only the SHA-256 of the
        # token is stored; the password is never stored here.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mt5_setup_tokens (
                token_hash TEXT PRIMARY KEY,
                login TEXT NOT NULL,
                discord_user TEXT DEFAULT '',
                job_id INTEGER DEFAULT 0,
                expires_ts REAL DEFAULT 0,
                used INTEGER DEFAULT 0,
                created_ts REAL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                created_ts REAL DEFAULT 0,
                sent INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT,
                time REAL,
                symbol TEXT,
                ticket TEXT,
                reason TEXT,
                volume TEXT,
                profit REAL
            )
        """)
        # Broker-side deals (manual trades, deposits, etc.) are display-only.
        # They deliberately live outside ``trades`` so they never touch the
        # daily loss count, block state, or GuardRails risk rules.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broker_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL,
                deal_id TEXT NOT NULL,
                position_id TEXT DEFAULT '',
                time REAL DEFAULT 0,
                symbol TEXT DEFAULT '',
                side TEXT DEFAULT '',
                volume TEXT DEFAULT '',
                profit REAL DEFAULT 0,
                source TEXT DEFAULT 'broker',
                created_ts REAL DEFAULT 0,
                UNIQUE(login, deal_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_broker_trades_login_ts
            ON broker_trades (login, time)
        """)
        # One row per (account, ticket). Older databases are already deduped by
        # the /event handler; skip the index if a legacy duplicate exists
        # instead of refusing to start.
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_login_ticket "
                "ON trades(login, ticket)"
            )
        except sqlite3.IntegrityError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # One-time OAuth links used by /addctrader. The token is stored hashed,
        # exactly like the MT5 provisioning links.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ctrader_connect_tokens (
                token_hash TEXT PRIMARY KEY,
                discord_user TEXT,
                phase TEXT,
                risk_pct REAL,
                expires_ts REAL DEFAULT 0,
                used INTEGER DEFAULT 0,
                created_ts REAL DEFAULT 0
            )
        """)
        # One OAuth grant per cTID. Tokens are encrypted with CREDENTIAL_KEY and
        # are never returned by the public Discord API.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ctrader_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user TEXT,
                phase TEXT,
                risk_pct REAL,
                environment TEXT DEFAULT 'demo',
                access_token_enc TEXT DEFAULT '',
                refresh_token_enc TEXT DEFAULT '',
                access_expires_at REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                created_ts REAL DEFAULT 0,
                updated_ts REAL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS equity_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL,
                timestamp REAL NOT NULL,
                equity REAL NOT NULL,
                balance REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_equity_snapshots_login_ts
            ON equity_snapshots (login, timestamp)
        """)
        # ---------------- copy trading ----------------
        # One row holds the selected master and the opt-in switch. Copying is
        # never enabled by code; only an explicit /copy start (or the PWA)
        # flips this on.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS copy_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                master_login TEXT DEFAULT '',
                enabled INTEGER DEFAULT 0,
                baselined INTEGER DEFAULT 0,
                updated_ts REAL DEFAULT 0
            )
        """)
        try:
            conn.execute("ALTER TABLE copy_config ADD COLUMN baselined INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "INSERT OR IGNORE INTO copy_config "
            "(id, master_login, enabled, baselined, updated_ts) VALUES (1, '', 0, 0, 0)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS copy_slaves (
                login TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                created_ts REAL DEFAULT 0
            )
        """)
        # One master trade (position or pending order). ``master_ticket`` is
        # the ticket used for close/cancel matching; ``master_order_ticket``
        # keeps the original pending-order id when a cTrader order fills into a
        # position with a different id.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS copy_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_login TEXT NOT NULL,
                master_ticket TEXT NOT NULL,
                master_order_ticket TEXT DEFAULT '',
                kind TEXT NOT NULL,
                symbol TEXT DEFAULT '',
                side TEXT DEFAULT '',
                entry TEXT DEFAULT '',
                sl TEXT DEFAULT '',
                status TEXT DEFAULT 'open',
                reason TEXT DEFAULT '',
                created_ts REAL DEFAULT 0,
                updated_ts REAL DEFAULT 0,
                UNIQUE(master_login, master_ticket, kind)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_copy_links_master_status
            ON copy_links (master_login, status)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS copy_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id INTEGER NOT NULL,
                slave_login TEXT NOT NULL,
                slave_ticket TEXT DEFAULT '',
                status TEXT DEFAULT 'queued',
                detail TEXT DEFAULT '',
                created_ts REAL DEFAULT 0,
                updated_ts REAL DEFAULT 0,
                UNIQUE(link_id, slave_login)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_copy_legs_slave_ticket
            ON copy_legs (slave_login, slave_ticket)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_copy_legs_status
            ON copy_legs (status)
        """)
        # Tickets already evaluated on the master. Persisting this prevents a
        # backend restart from re-copying every trade that is still open.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS copy_seen (
                master_login TEXT NOT NULL,
                ticket TEXT NOT NULL,
                kind TEXT NOT NULL,
                first_seen_ts REAL DEFAULT 0,
                PRIMARY KEY (master_login, ticket, kind)
            )
        """)
        conn.commit()


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def load_accounts() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM accounts").fetchall()
        return [dict(r) for r in rows]


def upsert_account(login, phase, risk_pct, suffix, day_start_balance, peak_equity,
                    already_blocked, last_loss_time, last_reset_date, daily_loss_count,
                    server="", instance_status="unprovisioned", instance_dir="",
                    provision_error="", bridge_token_hash="",
                    last_report_ts=0.0, platform="mt5", external_id="",
                    display_name="", account_environment="", account_type="",
                    grant_id=0):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO accounts (login, phase, risk_pct, suffix, day_start_balance, peak_equity,
                                   already_blocked, last_loss_time, last_reset_date, daily_loss_count,
                                   server, instance_status, instance_dir, provision_error,
                                   bridge_token_hash, last_report_ts, platform, external_id,
                                   display_name, account_environment, account_type, grant_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(login) DO UPDATE SET
                phase=excluded.phase,
                risk_pct=excluded.risk_pct,
                suffix=excluded.suffix,
                day_start_balance=excluded.day_start_balance,
                peak_equity=excluded.peak_equity,
                already_blocked=excluded.already_blocked,
                last_loss_time=excluded.last_loss_time,
                last_reset_date=excluded.last_reset_date,
                daily_loss_count=excluded.daily_loss_count,
                server=excluded.server,
                instance_status=excluded.instance_status,
                instance_dir=excluded.instance_dir,
                provision_error=excluded.provision_error,
                bridge_token_hash=excluded.bridge_token_hash,
                last_report_ts=excluded.last_report_ts,
                platform=excluded.platform,
                external_id=excluded.external_id,
                display_name=excluded.display_name,
                account_environment=excluded.account_environment,
                account_type=excluded.account_type,
                grant_id=excluded.grant_id
        """, (login, phase, risk_pct, suffix, day_start_balance, peak_equity,
              int(already_blocked), last_loss_time, last_reset_date, daily_loss_count,
              server, instance_status, instance_dir, provision_error,
              bridge_token_hash, last_report_ts, platform, external_id,
              display_name, account_environment, account_type, grant_id))
        conn.commit()


def delete_account(login):
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE login = ?", (login,))
        conn.execute("DELETE FROM account_symbols WHERE login = ?", (login,))
        conn.execute("DELETE FROM broker_trades WHERE login = ?", (login,))
        conn.execute("DELETE FROM mt5_setup_tokens WHERE login = ?", (login,))
        conn.commit()


def load_symbols(login: str) -> list[str]:
    """Bare symbol roots enabled for an account, in the order they were added."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT symbol FROM account_symbols WHERE login = ? ORDER BY rowid",
            (login,),
        ).fetchall()
        return [r["symbol"] for r in rows]


def add_symbols(login: str, symbols: list[str]):
    if not symbols:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO account_symbols (login, symbol) VALUES (?, ?)",
            [(login, symbol) for symbol in symbols],
        )
        conn.commit()


def remove_symbol(login: str, symbol: str):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM account_symbols WHERE login = ? AND symbol = ?",
            (login, symbol),
        )
        conn.commit()


def trade_exists(login: str, ticket: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM trades WHERE login = ? AND ticket = ? LIMIT 1",
            (login, str(ticket)),
        ).fetchone()
        return row is not None


def insert_trade(login, ts, symbol, ticket, reason, volume, profit) -> bool:
    """Insert a closed trade. Returns False when (login, ticket) already exists."""
    with get_conn() as conn:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO trades (login, time, symbol, ticket, reason, volume, profit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (login, ts, symbol, ticket, reason, volume, profit))
        conn.commit()
        return cursor.rowcount > 0


def load_trades(login: str = None) -> list[dict]:
    with get_conn() as conn:
        if login:
            rows = conn.execute("SELECT * FROM trades WHERE login = ? ORDER BY time", (login,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM trades ORDER BY time").fetchall()
        return [dict(r) for r in rows]


def upsert_broker_trades(login: str, rows: list[dict]) -> int:
    """Insert broker-side closed deals, ignoring duplicates by deal id.

    Returns the number of newly inserted rows. This table is display-only:
    callers must not use it for loss limits or blocking.
    """
    if not rows:
        return 0
    now = time.time()
    values = [
        (
            login,
            str(row.get("deal_id") or ""),
            str(row.get("position_id") or ""),
            float(row.get("time") or 0),
            str(row.get("symbol") or "")[:32],
            str(row.get("side") or "")[:16],
            str(row.get("volume") or "")[:32],
            float(row.get("profit") or 0),
            str(row.get("source") or "broker")[:16],
            now,
        )
        for row in rows
        if str(row.get("deal_id") or "").strip()
    ]
    if not values:
        return 0
    with get_conn() as conn:
        before = conn.total_changes
        conn.executemany("""
            INSERT OR IGNORE INTO broker_trades
                (login, deal_id, position_id, time, symbol, side, volume,
                 profit, source, created_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, values)
        conn.commit()
        return conn.total_changes - before


def load_broker_trades(login: str, limit: int = 500) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM broker_trades WHERE login = ? ORDER BY time DESC LIMIT ?",
            (login, max(1, int(limit))),
        ).fetchall()
        return [dict(r) for r in rows]


def insert_equity_snapshot(login, timestamp, equity, balance):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO equity_snapshots (login, timestamp, equity, balance)
            VALUES (?, ?, ?, ?)
        """, (login, timestamp, equity, balance))
        conn.commit()


def load_equity_snapshots(login: str, since_ts: float = 0.0, limit: int = 500) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT timestamp, equity, balance FROM equity_snapshots "
            "WHERE login = ? AND timestamp >= ? ORDER BY timestamp LIMIT ?",
            (login, since_ts, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def prune_equity_snapshots(before_ts: float):
    with get_conn() as conn:
        conn.execute("DELETE FROM equity_snapshots WHERE timestamp < ?", (before_ts,))
        conn.commit()


def get_setting(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))
        conn.commit()


def get_bridge_token_hash(login: str) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT bridge_token_hash FROM accounts WHERE login = ?", (login,)
        ).fetchone()
        return (row["bridge_token_hash"] or "") if row else ""


# ---------------- provisioning / agent persistence ----------------

_ACCOUNT_PROVISION_FIELDS = {
    "server", "instance_status", "instance_dir", "provision_error",
    "bridge_token_hash", "last_report_ts",
    "platform", "external_id", "display_name", "account_environment",
    "account_type", "grant_id",
}


def update_account_provision(login: str, **fields):
    """Update only the provisioning columns, leaving trading state untouched."""
    allowed = {key: value for key, value in fields.items() if key in _ACCOUNT_PROVISION_FIELDS}
    if not allowed:
        return
    assignments = ", ".join(f"{key} = ?" for key in allowed)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE accounts SET {assignments} WHERE login = ?",
            (*allowed.values(), login),
        )
        conn.commit()


def create_provision_job(login: str, action: str, payload_enc: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO provision_jobs (login, action, payload_enc, status, created_ts) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (login, action, payload_enc, time.time()),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_account(login: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE login = ?", (login,)).fetchone()
        return dict(row) if row else None


def get_provision_job(job_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM provision_jobs WHERE id = ?", (int(job_id),)
        ).fetchone()
        return dict(row) if row else None


def get_latest_provision_job(login: str, action: str = "mt5_login"):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM provision_jobs WHERE login = ? AND action = ? "
            "ORDER BY id DESC LIMIT 1",
            (login, action),
        ).fetchone()
        return dict(row) if row else None


def claim_next_provision_job():
    """Atomically claim the oldest pending provisioning job, if any."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM provision_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cur = conn.execute(
            "UPDATE provision_jobs SET status = 'claimed', claimed_ts = ? "
            "WHERE id = ? AND status = 'pending'",
            (time.time(), row["id"]),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        claimed = dict(row)
        claimed["status"] = "claimed"
        return claimed


def reset_stale_provision_jobs(older_than: float = 600.0):
    """Return jobs abandoned by a crashed watcher to the pending queue."""
    cutoff = time.time() - max(60.0, float(older_than))
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE provision_jobs SET status = 'pending', claimed_ts = 0 "
            "WHERE status = 'claimed' AND claimed_ts < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount


def create_mt5_setup_token(token_hash: str, login: str, discord_user: str,
                           expires_ts: float):
    with get_conn() as conn:
        # A new link supersedes any earlier unused link for the same login.
        conn.execute(
            "DELETE FROM mt5_setup_tokens WHERE login = ? AND used = 0",
            (login,),
        )
        conn.execute(
            "INSERT INTO mt5_setup_tokens "
            "(token_hash, login, discord_user, job_id, expires_ts, used, created_ts) "
            "VALUES (?, ?, ?, 0, ?, 0, ?)",
            (token_hash, login, discord_user, expires_ts, time.time()),
        )
        conn.commit()


def get_mt5_setup_token(token_hash: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM mt5_setup_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return dict(row) if row else None


def consume_mt5_setup_token(token_hash: str):
    """Atomically mark a setup link used. Returns the row or None."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE mt5_setup_tokens SET used = 1 "
            "WHERE token_hash = ? AND used = 0 AND expires_ts >= ?",
            (token_hash, time.time()),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT * FROM mt5_setup_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return dict(row) if row else None


def attach_mt5_setup_job(token_hash: str, job_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE mt5_setup_tokens SET job_id = ? WHERE token_hash = ?",
            (int(job_id), token_hash),
        )
        conn.commit()


def cancel_provision_jobs(login: str, action: str = "mt5_login") -> int:
    """Discard queued jobs for a login that is being removed."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE provision_jobs SET status = 'error', error = 'account removed', "
            "payload_enc = '' WHERE login = ? AND action = ? "
            "AND status IN ('pending', 'claimed')",
            (login, action),
        )
        conn.commit()
        return cur.rowcount


def complete_provision_job(job_id: int, status: str, result: str = "", error: str = ""):
    with get_conn() as conn:
        conn.execute(
            "UPDATE provision_jobs SET status = ?, completed_ts = ?, result = ?, error = ?, "
            "payload_enc = '' WHERE id = ?",
            (status, time.time(), result, error, job_id),
        )
        conn.commit()


def enqueue_notification(text: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notifications (text, created_ts, sent) VALUES (?, ?, 0)",
            (text, time.time()),
        )
        conn.commit()


def get_pending_notifications(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE sent = 0 ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]


def mark_notification_sent(notification_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE notifications SET sent = 1 WHERE id = ?", (notification_id,))
        conn.commit()


# ---------------- cTrader OAuth grants ----------------

def create_ctrader_connect_token(token_hash: str, discord_user: str, phase: str,
                                 risk_pct: float, expires_ts: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ctrader_connect_tokens "
            "(token_hash, discord_user, phase, risk_pct, expires_ts, used, created_ts) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (token_hash, discord_user, phase, risk_pct, expires_ts, time.time()),
        )
        conn.commit()


def get_ctrader_connect_token(token_hash: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ctrader_connect_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return dict(row) if row else None


def consume_ctrader_connect_token(token_hash: str):
    """Atomically mark an OAuth link used. Returns the row or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM ctrader_connect_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if not row or row["used"]:
            return None
        if float(row["expires_ts"]) < time.time():
            return None
        cur = conn.execute(
            "UPDATE ctrader_connect_tokens SET used = 1 WHERE token_hash = ? AND used = 0",
            (token_hash,),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        return dict(row)


def get_single_ctrader_connect_token():
    """Peek the only pending OAuth link, if there is exactly one.

    cTrader's authorize URL does not document an OAuth ``state`` echo, and a
    browser may drop the marker cookie (different browser, cookies disabled).
    Falling back to the sole pending link keeps /addctrader usable without
    guessing when more than one link is outstanding.
    """
    now = time.time()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ctrader_connect_tokens WHERE used = 0 AND expires_ts >= ?",
            (now,),
        ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None


def create_ctrader_grant(discord_user: str, phase: str, risk_pct: float,
                         environment: str, access_token_enc: str,
                         refresh_token_enc: str, access_expires_at: float) -> int:
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ctrader_grants "
            "(discord_user, phase, risk_pct, environment, access_token_enc, "
            " refresh_token_enc, access_expires_at, status, error, created_ts, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, ?)",
            (discord_user, phase, risk_pct, environment, access_token_enc,
             refresh_token_enc, access_expires_at, now, now),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_ctrader_grant(grant_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM ctrader_grants WHERE id = ?", (grant_id,)).fetchone()
        return dict(row) if row else None


def list_ctrader_grants(statuses: tuple[str, ...] = ("pending", "active")):
    with get_conn() as conn:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = conn.execute(
                f"SELECT * FROM ctrader_grants WHERE status IN ({placeholders}) ORDER BY id",
                statuses,
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ctrader_grants ORDER BY id").fetchall()
        return [dict(row) for row in rows]


def update_ctrader_grant_tokens(grant_id: int, access_token_enc: str,
                                refresh_token_enc: str, access_expires_at: float):
    with get_conn() as conn:
        conn.execute(
            "UPDATE ctrader_grants SET access_token_enc = ?, refresh_token_enc = ?, "
            "access_expires_at = ?, updated_ts = ? WHERE id = ? AND status != 'revoked'",
            (access_token_enc, refresh_token_enc, access_expires_at, time.time(), grant_id),
        )
        conn.commit()


def update_ctrader_grant_status(grant_id: int, status: str, error: str = ""):
    with get_conn() as conn:
        # ``revoked`` is terminal: the refresh token is already cleared, and a
        # late connector request must not flip the row back into discovery.
        conn.execute(
            "UPDATE ctrader_grants SET status = ?, error = ?, updated_ts = ? "
            "WHERE id = ? AND status != 'revoked'",
            (status, error[:500], time.time(), grant_id),
        )
        conn.commit()


def list_ctrader_accounts(grant_id: int | None = None):
    with get_conn() as conn:
        if grant_id is None:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE platform = 'ctrader' ORDER BY login"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM accounts WHERE platform = 'ctrader' AND grant_id = ? "
                "ORDER BY login", (grant_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def get_ctrader_account_by_external_id(external_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE platform = 'ctrader' AND external_id = ?",
            (str(external_id),),
        ).fetchone()
        return dict(row) if row else None


def get_ctrader_accounts_for_grant(grant_id: int):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE platform = 'ctrader' AND grant_id = ? ORDER BY login",
            (grant_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def revoke_ctrader_grant_if_empty(grant_id: int) -> bool:
    """Forget an OAuth grant once its last account is removed.

    The refresh token is cleared, not just marked revoked, so a removed account
    cannot be resurrected from stale credentials.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM accounts WHERE platform = 'ctrader' AND grant_id = ?",
            (grant_id,),
        ).fetchone()
        if row and row["n"]:
            return False
        conn.execute(
            "UPDATE ctrader_grants SET status = 'revoked', access_token_enc = '', "
            "refresh_token_enc = '', access_expires_at = 0, error = '', updated_ts = ? "
            "WHERE id = ?",
            (time.time(), grant_id),
        )
        conn.commit()
        return True


# ---------------- copy trading ----------------

_COPY_LINK_FIELDS = {
    "master_ticket", "master_order_ticket", "kind", "symbol", "side",
    "entry", "sl", "status", "reason", "updated_ts",
}
_COPY_LEG_FIELDS = {"slave_ticket", "status", "detail", "updated_ts"}


def get_copy_config() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM copy_config WHERE id = 1").fetchone()
        if row is None:
            now = time.time()
            conn.execute(
                "INSERT INTO copy_config "
                "(id, master_login, enabled, baselined, updated_ts) "
                "VALUES (1, '', 0, 0, ?)",
                (now,),
            )
            conn.commit()
            return {
                "id": 1, "master_login": "", "enabled": 0,
                "baselined": 0, "updated_ts": now,
            }
        return dict(row)


def set_copy_config(master_login: str, enabled: bool,
                    baselined: bool | None = None) -> dict:
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO copy_config "
            "(id, master_login, enabled, baselined, updated_ts) "
            "VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET master_login = excluded.master_login, "
            "enabled = excluded.enabled, "
            "baselined = COALESCE(?, baselined), "
            "updated_ts = excluded.updated_ts",
            (master_login or "", 1 if enabled else 0,
             0 if baselined is None else (1 if baselined else 0), now,
             None if baselined is None else (1 if baselined else 0)),
        )
        conn.commit()
    return get_copy_config()


def set_copy_baselined(value: bool):
    with get_conn() as conn:
        conn.execute(
            "UPDATE copy_config SET baselined = ?, updated_ts = ? WHERE id = 1",
            (1 if value else 0, time.time()),
        )
        conn.commit()


def list_copy_slaves(enabled_only: bool = False) -> list[dict]:
    with get_conn() as conn:
        if enabled_only:
            rows = conn.execute(
                "SELECT * FROM copy_slaves WHERE enabled = 1 ORDER BY created_ts, login"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM copy_slaves ORDER BY created_ts, login"
            ).fetchall()
        return [dict(row) for row in rows]


def add_copy_slave(login: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO copy_slaves (login, enabled, created_ts) VALUES (?, 1, ?) "
            "ON CONFLICT(login) DO UPDATE SET enabled = 1",
            (login, time.time()),
        )
        conn.commit()


def remove_copy_slave(login: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM copy_slaves WHERE login = ?", (login,))
        conn.commit()


def clear_copy_slaves():
    with get_conn() as conn:
        conn.execute("DELETE FROM copy_slaves")
        conn.commit()


def create_copy_link(master_login: str, master_ticket: str, kind: str,
                     symbol: str = "", side: str = "", entry: str = "",
                     sl: str = "", master_order_ticket: str = "") -> int:
    """Insert a link, returning the existing id when it is already known."""
    now = time.time()
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO copy_links "
                "(master_login, master_ticket, master_order_ticket, kind, symbol, "
                " side, entry, sl, status, reason, created_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', '', ?, ?)",
                (master_login, str(master_ticket), str(master_order_ticket or ""),
                 kind, symbol, side, str(entry), str(sl), now, now),
            )
            conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM copy_links WHERE master_login = ? "
                "AND master_ticket = ? AND kind = ?",
                (master_login, str(master_ticket), kind),
            ).fetchone()
            return int(row["id"]) if row else 0


def get_copy_link(link_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM copy_links WHERE id = ?", (int(link_id),)).fetchone()
        return dict(row) if row else None


def get_copy_link_by_master(master_login: str, master_ticket: str, kind: str | None = None):
    with get_conn() as conn:
        if kind:
            row = conn.execute(
                "SELECT * FROM copy_links WHERE master_login = ? "
                "AND master_ticket = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                (master_login, str(master_ticket), kind),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM copy_links WHERE master_login = ? "
                "AND master_ticket = ? ORDER BY id DESC LIMIT 1",
                (master_login, str(master_ticket)),
            ).fetchone()
        return dict(row) if row else None


def get_copy_link_by_order(master_login: str, master_order_ticket: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM copy_links WHERE master_login = ? "
            "AND master_order_ticket = ? ORDER BY id DESC LIMIT 1",
            (master_login, str(master_order_ticket)),
        ).fetchone()
        return dict(row) if row else None


def update_copy_link(link_id: int, **fields):
    allowed = {key: value for key, value in fields.items() if key in _COPY_LINK_FIELDS}
    if not allowed:
        return
    allowed.setdefault("updated_ts", time.time())
    assignments = ", ".join(f"{key} = ?" for key in allowed)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE copy_links SET {assignments} WHERE id = ?",
            (*allowed.values(), int(link_id)),
        )
        conn.commit()


def list_copy_links(master_login: str | None = None, statuses: tuple[str, ...] | None = None,
                    limit: int = 50) -> list[dict]:
    query = "SELECT * FROM copy_links"
    clauses, params = [], []
    if master_login:
        clauses.append("master_login = ?")
        params.append(master_login)
    if statuses:
        clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
        params.extend(statuses)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def close_copy_links(master_login: str, status: str = "cancelled",
                     reason: str = "master changed") -> int:
    now = time.time()
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE copy_links SET status = ?, reason = ?, updated_ts = ? "
            "WHERE master_login = ? AND status IN ('open', 'pending')",
            (status, reason, now, master_login),
        )
        conn.commit()
        return cur.rowcount


def add_copy_leg(link_id: int, slave_login: str) -> int:
    now = time.time()
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO copy_legs "
                "(link_id, slave_login, slave_ticket, status, detail, created_ts, updated_ts) "
                "VALUES (?, ?, '', 'queued', '', ?, ?)",
                (int(link_id), slave_login, now, now),
            )
            conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT id FROM copy_legs WHERE link_id = ? AND slave_login = ?",
                (int(link_id), slave_login),
            ).fetchone()
            return int(row["id"]) if row else 0


def update_copy_leg(leg_id: int, **fields):
    allowed = {key: value for key, value in fields.items() if key in _COPY_LEG_FIELDS}
    if not allowed:
        return
    allowed.setdefault("updated_ts", time.time())
    assignments = ", ".join(f"{key} = ?" for key in allowed)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE copy_legs SET {assignments} WHERE id = ?",
            (*allowed.values(), int(leg_id)),
        )
        conn.commit()


def get_copy_legs(link_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM copy_legs WHERE link_id = ? ORDER BY id", (int(link_id),)
        ).fetchall()
        return [dict(row) for row in rows]


def list_copy_legs_for_slave(slave_login: str, statuses: tuple[str, ...] = ()) -> list[dict]:
    """Legs joined with their master link, oldest first."""
    query = (
        "SELECT l.*, k.master_login, k.master_ticket, k.master_order_ticket, "
        "k.kind AS link_kind, k.symbol AS link_symbol, k.side AS link_side, "
        "k.entry AS link_entry, k.sl AS link_sl, k.status AS link_status, "
        "k.reason AS link_reason "
        "FROM copy_legs l JOIN copy_links k ON k.id = l.link_id "
        "WHERE l.slave_login = ?"
    )
    params: list = [slave_login]
    if statuses:
        query += f" AND l.status IN ({','.join('?' for _ in statuses)})"
        params.extend(statuses)
    query += " ORDER BY l.id"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def find_copy_leg_by_ticket(slave_login: str, slave_ticket: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT l.*, k.master_login, k.master_ticket, k.master_order_ticket, "
            "k.kind AS link_kind, k.symbol AS link_symbol, k.side AS link_side, "
            "k.entry AS link_entry, k.sl AS link_sl, k.status AS link_status, "
            "k.reason AS link_reason "
            "FROM copy_legs l JOIN copy_links k ON k.id = l.link_id "
            "WHERE l.slave_login = ? AND l.slave_ticket = ? "
            "ORDER BY l.id DESC LIMIT 1",
            (slave_login, str(slave_ticket)),
        ).fetchone()
        return dict(row) if row else None


def load_copy_seen(master_login: str) -> set[tuple[str, str]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT ticket, kind FROM copy_seen WHERE master_login = ?", (master_login,)
        ).fetchall()
        return {(str(row["ticket"]), str(row["kind"])) for row in rows}


def add_copy_seen(master_login: str, ticket: str, kind: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO copy_seen (master_login, ticket, kind, first_seen_ts) "
            "VALUES (?, ?, ?, ?)",
            (master_login, str(ticket), kind, time.time()),
        )
        conn.commit()


def add_copy_seen_many(master_login: str, items) -> None:
    now = time.time()
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO copy_seen (master_login, ticket, kind, first_seen_ts) "
            "VALUES (?, ?, ?, ?)",
            [(master_login, str(ticket), kind, now) for ticket, kind in items],
        )
        conn.commit()


def clear_copy_seen(master_login: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM copy_seen WHERE master_login = ?", (master_login,))
        conn.commit()


def forget_copy_slave_legs(slave_login: str):
    """Stop matching new events to a slave that was removed from copy config.

    Existing broker positions are deliberately left alone; only the local
    bookkeeping is marked skipped so later events cannot resurrect a link.
    """
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            "UPDATE copy_legs SET status = 'skipped', detail = 'slave removed', "
            "updated_ts = ? WHERE slave_login = ? AND status IN ('queued', 'pending')",
            (now, slave_login),
        )
        conn.commit()

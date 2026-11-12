"""
SQLite persistence for the bot backend. No separate database server needed â€”
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
            ("agent_id", "TEXT DEFAULT ''"),
            ("bridge_token_hash", "TEXT DEFAULT ''"),
            ("last_report_ts", "REAL DEFAULT 0"),
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
        # Windows agent jobs. payload_enc holds the encrypted JSON payload
        # (MT5 password, per-instance bridge token) and is cleared as soon as
        # the agent reports a result.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provision_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_enc TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                agent_id TEXT DEFAULT '',
                created_ts REAL DEFAULT 0,
                claimed_ts REAL DEFAULT 0,
                completed_ts REAL DEFAULT 0,
                result TEXT DEFAULT '',
                error TEXT DEFAULT ''
            )
        """)
        # Single-use links for the secure password form. Only a hash of the
        # token is stored, so a database leak does not expose live links.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS provision_tokens (
                token_hash TEXT PRIMARY KEY,
                login TEXT NOT NULL,
                expires_ts REAL NOT NULL,
                used INTEGER DEFAULT 0,
                created_ts REAL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_state (
                agent_id TEXT PRIMARY KEY,
                hostname TEXT DEFAULT '',
                stats TEXT DEFAULT '{}',
                last_seen REAL DEFAULT 0
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
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
                    provision_error="", agent_id="", bridge_token_hash="",
                    last_report_ts=0.0):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO accounts (login, phase, risk_pct, suffix, day_start_balance, peak_equity,
                                   already_blocked, last_loss_time, last_reset_date, daily_loss_count,
                                   server, instance_status, instance_dir, provision_error, agent_id,
                                   bridge_token_hash, last_report_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                agent_id=excluded.agent_id,
                bridge_token_hash=excluded.bridge_token_hash,
                last_report_ts=excluded.last_report_ts
        """, (login, phase, risk_pct, suffix, day_start_balance, peak_equity,
              int(already_blocked), last_loss_time, last_reset_date, daily_loss_count,
              server, instance_status, instance_dir, provision_error, agent_id,
              bridge_token_hash, last_report_ts))
        conn.commit()


def delete_account(login):
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE login = ?", (login,))
        conn.execute("DELETE FROM account_symbols WHERE login = ?", (login,))
        conn.execute("DELETE FROM provision_tokens WHERE login = ?", (login,))
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


def insert_trade(login, ts, symbol, ticket, reason, volume, profit):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO trades (login, time, symbol, ticket, reason, volume, profit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (login, ts, symbol, ticket, reason, volume, profit))
        conn.commit()


def load_trades(login: str = None) -> list[dict]:
    with get_conn() as conn:
        if login:
            rows = conn.execute("SELECT * FROM trades WHERE login = ? ORDER BY time", (login,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM trades ORDER BY time").fetchall()
        return [dict(r) for r in rows]


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
    "agent_id", "bridge_token_hash", "last_report_ts",
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


def create_provision_token(token_hash: str, login: str, expires_ts: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO provision_tokens (token_hash, login, expires_ts, used, created_ts) "
            "VALUES (?, ?, ?, 0, ?)",
            (token_hash, login, expires_ts, time.time()),
        )
        conn.commit()


def get_provision_token(token_hash: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM provision_tokens WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        return dict(row) if row else None


def consume_provision_token(token_hash: str):
    """Atomically mark a one-time token used. Returns the login or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT login, expires_ts, used FROM provision_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not row or row["used"]:
            return None
        if float(row["expires_ts"]) < time.time():
            return None
        cur = conn.execute(
            "UPDATE provision_tokens SET used = 1 WHERE token_hash = ? AND used = 0",
            (token_hash,),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        return row["login"]


def create_provision_job(login: str, action: str, payload_enc: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO provision_jobs (login, action, payload_enc, status, created_ts) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (login, action, payload_enc, time.time()),
        )
        conn.commit()
        return int(cur.lastrowid)


def claim_next_job(agent_id: str):
    """Claim one pending job atomically. Returns the row (with payload_enc)."""
    # A claimed job whose agent vanished (crash, reboot, lost network) would
    # otherwise be stuck forever. Requeue after ten minutes.
    with get_conn() as conn:
        conn.execute(
            "UPDATE provision_jobs SET status = 'pending', agent_id = '', claimed_ts = 0 "
            "WHERE status = 'claimed' AND claimed_ts < ?",
            (time.time() - 600,),
        )
        conn.commit()
    for _ in range(3):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM provision_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cur = conn.execute(
                "UPDATE provision_jobs SET status = 'claimed', agent_id = ?, claimed_ts = ? "
                "WHERE id = ? AND status = 'pending'",
                (agent_id, time.time(), row["id"]),
            )
            conn.commit()
            if cur.rowcount == 1:
                return dict(row)
    return None


def complete_provision_job(job_id: int, status: str, result: str = "", error: str = ""):
    with get_conn() as conn:
        conn.execute(
            "UPDATE provision_jobs SET status = ?, completed_ts = ?, result = ?, error = ?, "
            "payload_enc = '' WHERE id = ?",
            (status, time.time(), result, error, job_id),
        )
        conn.commit()


def get_provision_job(job_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM provision_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def get_pending_job_for_login(login: str, actions: tuple[str, ...] = ()):
    with get_conn() as conn:
        if actions:
            placeholders = ",".join("?" for _ in actions)
            row = conn.execute(
                f"SELECT * FROM provision_jobs WHERE login = ? AND status IN ('pending','claimed') "
                f"AND action IN ({placeholders}) ORDER BY id DESC LIMIT 1",
                (login, *actions),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM provision_jobs WHERE login = ? AND status IN ('pending','claimed') "
                "ORDER BY id DESC LIMIT 1",
                (login,),
            ).fetchone()
        return dict(row) if row else None


def upsert_agent_state(agent_id: str, hostname: str, stats: str, last_seen: float):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO agent_state (agent_id, hostname, stats, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                hostname=excluded.hostname, stats=excluded.stats, last_seen=excluded.last_seen
        """, (agent_id, hostname, stats, last_seen))
        conn.commit()


def get_agent_state(agent_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM agent_state WHERE agent_id = ?", (agent_id,)).fetchone()
        return dict(row) if row else None


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

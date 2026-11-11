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
SIGNAL_RETENTION_SECONDS = 30 * 24 * 3600  # keep raw webhook captures for 30 days


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
        # Per-account symbol whitelist. The backend stores bare broker roots
        # (EURUSD, XAUUSD, ...); each EA appends its own configured suffix.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS account_symbols (
                login TEXT NOT NULL,
                symbol TEXT NOT NULL,
                PRIMARY KEY (login, symbol)
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
        # Inbound webhook signals. The event_key is a content hash used to
        # collapse retries from alerting platforms; raw is the redacted
        # payload kept for debugging vendor schemas (OpenMarket et al).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_events (
                event_key TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                updated_at REAL,
                status TEXT NOT NULL,
                login TEXT,
                symbol TEXT,
                side TEXT,
                sl REAL,
                message TEXT,
                note TEXT,
                raw TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_signal_events_created
            ON signal_events (created_at DESC)
        """)
        conn.execute(
            "DELETE FROM signal_events WHERE created_at < ?",
            (time.time() - SIGNAL_RETENTION_SECONDS,),
        )
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
                    already_blocked, last_loss_time, last_reset_date, daily_loss_count):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO accounts (login, phase, risk_pct, suffix, day_start_balance, peak_equity,
                                   already_blocked, last_loss_time, last_reset_date, daily_loss_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(login) DO UPDATE SET
                phase=excluded.phase,
                risk_pct=excluded.risk_pct,
                suffix=excluded.suffix,
                day_start_balance=excluded.day_start_balance,
                peak_equity=excluded.peak_equity,
                already_blocked=excluded.already_blocked,
                last_loss_time=excluded.last_loss_time,
                last_reset_date=excluded.last_reset_date,
                daily_loss_count=excluded.daily_loss_count
        """, (login, phase, risk_pct, suffix, day_start_balance, peak_equity,
              int(already_blocked), last_loss_time, last_reset_date, daily_loss_count))
        conn.commit()


def delete_account(login):
    with get_conn() as conn:
        conn.execute("DELETE FROM accounts WHERE login = ?", (login,))
        conn.execute("DELETE FROM account_symbols WHERE login = ?", (login,))
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


def claim_signal(event_key, ts, login=None, symbol=None, side=None, sl=None,
                 message="", raw="", window_seconds=300) -> bool:
    """Atomically claim a signal event for processing.

    Returns False when an identical event was already claimed inside
    ``window_seconds``; that is a platform retry and should be ignored.
    After the window an identical signal is allowed again, so a strategy
    that legitimately fires the same price/SL later still trades.
    """
    with get_conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT created_at FROM signal_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if row is not None and 0 <= ts - float(row["created_at"]) < window_seconds:
                conn.rollback()
                return False
            conn.execute("""
                INSERT INTO signal_events
                    (event_key, created_at, updated_at, status, login, symbol,
                     side, sl, message, note, raw)
                VALUES (?, ?, ?, 'received', ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(event_key) DO UPDATE SET
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    status='received',
                    login=excluded.login,
                    symbol=excluded.symbol,
                    side=excluded.side,
                    sl=excluded.sl,
                    message=excluded.message,
                    note=NULL,
                    raw=excluded.raw
            """, (event_key, ts, ts, login, symbol, side, sl, message, raw))
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def update_signal(event_key, status, note=None):
    with get_conn() as conn:
        conn.execute("""
            UPDATE signal_events
            SET status = ?, note = ?, updated_at = ?
            WHERE event_key = ?
        """, (status, note, time.time(), event_key))
        conn.commit()


def recent_signals(limit: int = 20) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT event_key, created_at, updated_at, status, login, symbol,
                   side, sl, message, note, raw
            FROM signal_events
            ORDER BY created_at DESC
            LIMIT ?
        """, (max(1, min(int(limit), 200)),)).fetchall()
        return [dict(r) for r in rows]

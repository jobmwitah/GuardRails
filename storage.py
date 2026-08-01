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

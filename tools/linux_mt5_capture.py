#!/usr/bin/env python3
"""Wait for a fresh MT5 provisioning payload and apply it to the Linux terminal.

Runs on the VPS as root. It never writes the MT5 password to its log. The
password only exists transiently in start.ini while MT5 logs in, then is
scrubbed. The encrypted job is left pending on failure so a fixed retry can
reuse it; it is cleared only after MT5 authorizes and the EA reports.
"""

from __future__ import annotations

import fcntl
import grp
import os
import pwd
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path

BASE = Path("/opt/guardrails")
LOGIN = "61603648"
INSTANCE = BASE / "mt5" / LOGIN
START = INSTANCE / "start.ini"
PRESET = INSTANCE / "MQL5" / "Presets" / "EA_Bridge.set"
LOG = Path("/var/log/guardrails-mt5-capture.log")
LOCK = Path("/run/guardrails-mt5-capture.lock")
WATCH_SECONDS = 3600
CONNECT_SECONDS = 240
LOCAL_BACKEND = "http://127.0.0.1:8000"


def log(message: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        os.chmod(LOG, 0o600)
    except OSError:
        pass


def load_env():
    for raw in (BASE / ".env").read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def run(cmd, env=None, timeout=60):
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=timeout
        )
    except Exception as exc:  # noqa: BLE001 - commands are best-effort
        log(f"command error: {' '.join(cmd)}: {type(exc).__name__}")
        return None
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().replace("\n", " ")[:300]
        log(f"command rc={res.returncode}: {' '.join(cmd)} {detail}")
    return res


def guardrails_ids():
    entry = pwd.getpwnam("guardrails")
    group = grp.getgrnam("guardrails")
    return entry.pw_uid, group.gr_gid


def read_text(path: Path) -> str:
    text = path.read_bytes().decode("utf-16")
    return text.lstrip("\ufeff")


def write_text(path: Path, text: str):
    uid, gid = guardrails_ids()
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    os.chmod(path, 0o600)
    os.chown(path, uid, gid)


def scrub_password_only():
    """Remove any leftover plaintext password without touching other keys."""
    if not START.exists():
        return
    lines = read_text(START).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = []
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.lower()
        if section == "[common]" and re.match(
            r"(?i)^(password|certpassword|proxypassword|mql5password)\s*=", stripped
        ):
            continue
        kept.append(line)
    write_text(START, "\r\n".join(kept).rstrip() + "\r\n")


def set_key(lines: list[str], section_name: str, key: str, value: str) -> list[str]:
    out: list[str] = []
    section = ""
    done = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if section == section_name and not done:
                out.append(f"{key}={value}")
                done = True
            section = stripped.lower()
            out.append(line)
            continue
        if section == section_name and re.match(rf"(?i)^{re.escape(key)}\s*=", stripped):
            out.append(f"{key}={value}")
            done = True
            continue
        out.append(line)
    if section == section_name and not done:
        out.append(f"{key}={value}")
    return out


def write_start_with_password(server: str, password: str):
    lines = read_text(START).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = set_key(lines, "[common]", "Login", LOGIN)
    lines = set_key(lines, "[common]", "Server", server)
    lines = set_key(lines, "[common]", "KeepPrivate", "1")
    lines = set_key(lines, "[experts]", "WebRequestUrl", LOCAL_BACKEND)

    out: list[str] = []
    section = ""
    inserted = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.lower()
        out.append(line)
        if (
            section == "[common]"
            and re.match(r"(?i)^login\s*=", stripped)
            and not inserted
        ):
            out.append(f"Password={password}")
            inserted = True
    if not inserted:
        raise RuntimeError("could not find [Common] Login in start.ini")
    write_text(START, "\r\n".join(out).rstrip() + "\r\n")


def write_preset(bridge_token: str, suffix: str):
    values = [
        ("BackendURL", LOCAL_BACKEND),
        ("AccountToken", bridge_token),
        ("BridgeDir", "guardrails"),
        ("AccountLoginOverride", LOGIN),
        ("PollSeconds", "5"),
        ("MagicNumber", "990011"),
        ("ExternalMagicMin", "100000"),
        ("ExternalMagicMax", "119999"),
        ("MarginCapPct", "80.0"),
        ("PriceTolerancePoints", "10.0"),
        ("SymbolSuffix", suffix or "0"),
        ("FastPollMs", "1000"),
        ("CloseAggregationMs", "1500"),
    ]
    text = "\r\n".join(f"{key}={value}" for key, value in values) + "\r\n"
    write_text(PRESET, text)


def latest_log(logs_dir: Path):
    if not logs_dir.exists():
        return "", None
    files = sorted(
        (p for p in logs_dir.glob("*.log") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        return "", None
    path = files[-1]
    try:
        return path.read_bytes().decode("utf-16", errors="replace"), path
    except OSError:
        return "", path


def new_lines(before_path, before_count: int, text: str, path):
    if path != before_path:
        return text.splitlines()
    return text.splitlines()[before_count:]


def redact(line: str) -> str:
    line = re.sub(r"(?i)(token|password|secret)=[^&\s]+", r"\1=<redacted>", line)
    line = re.sub(r"[A-Za-z0-9_-]{32,}", "<redacted>", line)
    return line.strip()[:300]


def find_job(since_ts: float):
    conn = sqlite3.connect(str(BASE / "bot_data.db"))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM provision_jobs WHERE login = ? "
            "AND status IN ('pending','claimed') AND payload_enc != '' "
            "AND created_ts >= ? ORDER BY id DESC LIMIT 1",
            (LOGIN, since_ts - 60),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def account_report_ts() -> float:
    conn = sqlite3.connect(str(BASE / "bot_data.db"))
    try:
        row = conn.execute(
            "SELECT last_report_ts FROM accounts WHERE login = ?", (LOGIN,)
        ).fetchone()
    finally:
        conn.close()
    return float(row[0] or 0) if row else 0.0


def main() -> int:
    lock_fd = open(LOCK, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another capture watcher is already running")
        return 0

    load_env()
    os.chdir(BASE)
    sys.path.insert(0, str(BASE))
    import provisioning  # noqa: E402
    import storage as db  # noqa: E402

    log("watcher started")
    try:
        scrub_password_only()
    except Exception:
        log("could not scrub stale start.ini password: " + traceback.format_exc(limit=1))

    watch_start = time.time()
    deadline = watch_start + WATCH_SECONDS
    job = None
    while time.time() < deadline:
        job = find_job(watch_start)
        if job:
            break
        time.sleep(5)
    if not job:
        log("WATCH_TIMEOUT no provisioning payload arrived")
        return 0

    try:
        payload = provisioning.decrypt_json(job["payload_enc"])
    except Exception as exc:
        log(f"payload decrypt failed: {type(exc).__name__}")
        return 2

    password = str(payload.get("password") or "")
    server = str(payload.get("server") or "").strip()
    bridge_token = str(payload.get("bridge_token") or "")
    suffix = str(payload.get("suffix") or "0").strip() or "0"
    log(
        f"payload received job={job['id']} server={server} "
        f"password_len={len(password)} token_len={len(bridge_token)}"
    )
    if not password or not server or not bridge_token:
        log("CAPTURE_FAIL payload is missing password/server/bridge token")
        return 2
    if "\r" in password or "\n" in password:
        log("CAPTURE_FAIL password contains a newline and cannot be written to start.ini")
        return 2

    uid, gid = guardrails_ids()
    os.chown(INSTANCE, uid, gid)
    try:
        os.chmod(START, 0o600)
    except OSError:
        pass

    log_before, log_path_before = latest_log(INSTANCE / "logs")
    log_count = len(log_before.splitlines())
    ea_before, ea_path_before = latest_log(INSTANCE / "MQL5" / "Logs")
    ea_count = len(ea_before.splitlines())

    auth = False
    ea_loaded = False
    report = False
    started_at = time.time()
    try:
        write_preset(bridge_token, suffix)
        write_start_with_password(server, password)
        log("start.ini updated with one-time password; starting MT5")

        wine_env = os.environ.copy()
        wine_env.update(
            HOME=str(BASE),
            WINEPREFIX=str(BASE / "mt5" / "wineprefix-staging"),
            WINEDEBUG="-all",
            WINEDLLOVERRIDES="mscoree,mshtml=",
            DISPLAY=":1",
        )
        run(["systemctl", "stop", "guardrails-mt5"])
        run(["systemctl", "stop", "guardrails-xvfb"])
        run(["pkill", "-x", "terminal64.exe"])
        run(["/opt/wine-staging/bin/wineserver", "-k"], env=wine_env)
        run(["pkill", "-x", "Xvfb"])
        time.sleep(3)
        run(["pkill", "-9", "-x", "terminal64.exe"])
        for stale in ("/tmp/.X1-lock", "/tmp/.X11-unix/X1"):
            try:
                os.unlink(stale)
            except OSError:
                pass

        res = run(["systemctl", "start", "guardrails-xvfb"])
        if res is None or res.returncode != 0:
            raise RuntimeError("could not start Xvfb")
        time.sleep(2)
        res = run(["systemctl", "start", "guardrails-mt5"])
        if res is None or res.returncode != 0:
            raise RuntimeError("could not start MT5 service")

        connect_deadline = time.time() + CONNECT_SECONDS
        while time.time() < connect_deadline:
            log_text, log_path = latest_log(INSTANCE / "logs")
            for line in new_lines(log_path_before, log_count, log_text, log_path):
                if f"'{LOGIN}': authorized on {server}" in line:
                    auth = True
                if "EA_Bridge" in line and "loaded successfully" in line:
                    ea_loaded = True

            ea_text, ea_path = latest_log(INSTANCE / "MQL5" / "Logs")
            if not ea_loaded:
                for line in new_lines(ea_path_before, ea_count, ea_text, ea_path):
                    if "EA_Bridge" in line and ("loaded" in line or "initialized" in line):
                        ea_loaded = True

            if account_report_ts() >= started_at - 5:
                report = True
            if auth and report:
                break
            time.sleep(5)

        if auth and report:
            scrub_password_only()
            db.update_account_provision(
                LOGIN,
                instance_status="running",
                instance_dir=str(INSTANCE),
                provision_error="",
            )
            db.complete_provision_job(
                job["id"], "done", result="linux-wine-staging"
            )
            run(["systemctl", "enable", "guardrails-xvfb", "guardrails-mt5"])
            age = round(time.time() - account_report_ts(), 1)
            log(
                f"CAPTURE_OK job={job['id']} auth={int(auth)} "
                f"ea={int(ea_loaded)} report={int(report)} report_age={age}s"
            )
            return 0

        db.update_account_provision(
            LOGIN,
            instance_status="error",
            instance_dir=str(INSTANCE),
            provision_error="MT5 Linux login/report did not complete; inspect capture log",
        )
        log(
            f"CAPTURE_FAIL job={job['id']} auth={int(auth)} "
            f"ea={int(ea_loaded)} report={int(report)}"
        )
        terminal_text, terminal_path = latest_log(INSTANCE / "logs")
        for line in new_lines(log_path_before, log_count, terminal_text, terminal_path)[-15:]:
            log("mt5: " + redact(line))
        ea_text, ea_path = latest_log(INSTANCE / "MQL5" / "Logs")
        for line in new_lines(ea_path_before, ea_count, ea_text, ea_path)[-15:]:
            log("ea: " + redact(line))
        return 3
    except Exception:
        log("CAPTURE_FAIL exception: " + traceback.format_exc(limit=3).replace("\n", " | "))
        return 3
    finally:
        try:
            scrub_password_only()
        except Exception:
            log("final password scrub failed; manual check required")
        # The job and its encrypted payload stay pending on failure so a fixed
        # retry can reuse it. On success complete_provision_job already cleared it.


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Relay the MT5 EA file bridge to the local GuardRails backend.

The EA writes URL-encoded request bodies into MQL5/Files/guardrails when MT5's
WebRequest URL whitelist cannot be configured. This process runs beside the
terminal, uploads those bodies to the backend, and writes /commands replies
back as cmd_*.txt files for the EA to consume.
"""

from __future__ import annotations

import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOGIN = os.environ.get("MT5_LOGIN", "61603648")
BRIDGE_DIR = Path(
    os.environ.get(
        "MT5_BRIDGE_DIR",
        f"/opt/guardrails/mt5/{LOGIN}/MQL5/Files/guardrails",
    )
)
PRESET = Path(
    os.environ.get(
        "MT5_PRESET",
        f"/opt/guardrails/mt5/{LOGIN}/MQL5/Presets/EA_Bridge.set",
    )
)
BACKEND = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
POLL_SECONDS = float(os.environ.get("BRIDGE_POLL_SECONDS", "1.0"))


def log(message: str):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def bridge_token() -> str:
    try:
        text = PRESET.read_bytes().decode("utf-16", errors="replace").lstrip("\ufeff")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("AccountToken="):
            return line.split("=", 1)[1].strip()
    return ""


def post_form(path: str, body: bytes) -> int:
    request = urllib.request.Request(
        BACKEND + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "GuardRails-MT5-Bridge/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def get_text(path: str) -> str:
    request = urllib.request.Request(
        BACKEND + path,
        method="GET",
        headers={"User-Agent": "GuardRails-MT5-Bridge/1.0"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read().decode("utf-8", "replace")


def upload_bodies():
    if not BRIDGE_DIR.is_dir():
        return
    for path in list(BRIDGE_DIR.glob("*.body")):
        name = path.name
        try:
            body = path.read_bytes()
        except OSError:
            continue
        if not body:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if name == "report.body":
            endpoint = "/report"
        elif name == "history.body":
            endpoint = "/mt5/history"
        elif name.startswith("candles_"):
            endpoint = "/candles"
        elif name.startswith("event_"):
            endpoint = "/event"
        else:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        try:
            status = post_form(endpoint, body)
        except urllib.error.HTTPError as exc:
            log(f"upload {name} -> HTTP {exc.code}")
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"upload {name} failed: {type(exc).__name__}")
            continue
        except Exception as exc:  # noqa: BLE001
            log(f"upload {name} error: {type(exc).__name__}")
            continue
        if 200 <= status < 300:
            try:
                path.unlink()
            except OSError:
                pass


def poll_commands(token: str):
    query = urllib.parse.urlencode({"token": token, "account": LOGIN})
    try:
        response = get_text("/commands?" + query)
    except urllib.error.HTTPError as exc:
        log(f"command poll HTTP {exc.code}")
        return
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log(f"command poll failed: {type(exc).__name__}")
        return
    except Exception as exc:  # noqa: BLE001
        log(f"command poll error: {type(exc).__name__}")
        return
    if not response.strip():
        return
    try:
        BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
        name = f"cmd_{time.time_ns()}.txt"
        tmp = BRIDGE_DIR / (name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(response)
        os.replace(tmp, BRIDGE_DIR / name)
    except OSError as exc:
        log(f"command write failed: {type(exc).__name__}")
        try:
            tmp.unlink()
        except OSError:
            pass


def main() -> int:
    log(f"bridge relay started login={LOGIN} backend={BACKEND}")
    token = ""
    token_ts = 0.0
    while True:
        try:
            if not token or time.time() - token_ts > 30:
                token = bridge_token()
                token_ts = time.time()
                if token:
                    log("bridge token loaded")
            upload_bodies()
            if token:
                poll_commands(token)
        except Exception as exc:  # noqa: BLE001
            log(f"relay loop error: {type(exc).__name__}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

"""Pure helpers for first-login MT5 provisioning on the Linux VPS.

The privileged watcher in ``tools/linux_mt5_provision.py`` uses these helpers;
keeping them pure makes the risky parts (start.ini parsing, password scrub,
preset generation) testable without Wine or systemd.
"""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from urllib.parse import urlparse

LOGIN_RE = re.compile(r"^\d{4,12}$")
PASSWORD_KEYS = re.compile(
    r"(?i)^(password|certpassword|proxypassword|mql5password)\s*="
)


def validate_login(value: str) -> str:
    login = str(value or "").strip()
    if not LOGIN_RE.match(login):
        raise ValueError("login must be 4–12 digits")
    return login


def validate_server(value: str) -> str:
    server = str(value or "").strip()
    if not server or len(server) > 120:
        raise ValueError("server must be 1–120 characters")
    if any(ch in server for ch in "\r\n\x00"):
        raise ValueError("server contains an invalid character")
    return server


def validate_password(value: str) -> str:
    password = str(value or "")
    if not password or len(password) > 128:
        raise ValueError("password must be 1–128 characters")
    if any(ch in password for ch in "\r\n\x00"):
        raise ValueError("password contains an invalid character")
    return password


def validate_broker_url(value: str) -> str:
    """Validate an optional broker MT5 installer URL.

    Some brokers ship a branded terminal whose ``Config/servers.dat`` is the
    only way to resolve their server names. The setup form accepts the
    broker's official ``.exe`` download link so the provisioner can cache that
    server list. Empty means "use the shared template".
    """
    url = str(value or "").strip()
    if not url:
        return ""
    if len(url) > 500:
        raise ValueError("broker download link is too long")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("broker download link must be an https URL")
    if not parsed.path.lower().endswith(".exe"):
        raise ValueError("broker download link must point to a .exe installer")
    host = parsed.hostname.lower()
    # The installer runs under Wine on the VPS, so only accept MetaQuotes'
    # official download host. Brokers' branded terminals are published there.
    if host != "download.mql5.com":
        raise ValueError(
            "broker download link must be on download.mql5.com"
        )
    return url


def read_ini(path: Path) -> list[str]:
    text = Path(path).read_bytes().decode("utf-16")
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def write_ini(path: Path, lines: list[str], uid: int | None = None,
              gid: int | None = None):
    text = "\r\n".join(lines).rstrip() + "\r\n"
    data = b"\xff\xfe" + text.encode("utf-16-le")
    path = Path(path)
    path.write_bytes(data)
    os.chmod(path, 0o600)
    if uid is not None and gid is not None:
        os.chown(path, uid, gid)


def set_key(lines: list[str], section_name: str, key: str, value: str) -> list[str]:
    """Set a key in a section, adding it if missing. Section names are literal."""
    target = section_name.lower()
    out: list[str] = []
    section = ""
    done = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if section == target and not done:
                out.append(f"{key}={value}")
                done = True
            section = stripped.lower()
            out.append(line)
            continue
        if section == target and re.match(rf"(?i)^{re.escape(key)}\s*=", stripped):
            out.append(f"{key}={value}")
            done = True
            continue
        out.append(line)
    if section == target and not done:
        out.append(f"{key}={value}")
    return out


def scrub_password_lines(lines: list[str]) -> list[str]:
    """Remove plaintext credential lines without touching anything else."""
    kept: list[str] = []
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.lower()
        if section == "[common]" and PASSWORD_KEYS.match(stripped):
            continue
        kept.append(line)
    return kept


def with_one_time_password(lines: list[str], password: str) -> list[str]:
    """Insert ``Password=`` after [Common] Login, replacing any old value."""
    cleaned = scrub_password_lines(lines)
    out: list[str] = []
    section = ""
    inserted = False
    for line in cleaned:
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
        raise ValueError("could not find [Common] Login in start.ini")
    return out


def render_start_ini(base_lines: list[str], login: str, server: str,
                     password: str | None = None) -> list[str]:
    lines = list(base_lines)
    lines = set_key(lines, "[Common]", "Login", login)
    lines = set_key(lines, "[Common]", "Server", server)
    lines = set_key(lines, "[Common]", "KeepPrivate", "1")
    lines = set_key(lines, "[Experts]", "WebRequestUrl", "http://127.0.0.1:8000")
    lines = set_key(lines, "[Experts]", "WebRequest", "1")
    lines = set_key(lines, "[Experts]", "AllowLiveTrading", "1")
    lines = set_key(lines, "[Experts]", "AllowDllImport", "0")
    lines = set_key(lines, "[Experts]", "Enabled", "1")
    lines = set_key(lines, "[StartUp]", "Expert", "EA_Bridge")
    lines = set_key(lines, "[StartUp]", "ExpertParameters", "EA_Bridge.set")
    lines = scrub_password_lines(lines)
    if password is not None:
        lines = with_one_time_password(lines, password)
    return lines


def render_preset(bridge_token: str, suffix: str, login: str) -> str:
    values = [
        ("BackendURL", "http://127.0.0.1:8000"),
        ("AccountToken", bridge_token),
        ("BridgeDir", "guardrails"),
        ("AccountLoginOverride", login),
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
    return "\r\n".join(f"{key}={value}" for key, value in values) + "\r\n"


def write_preset(path: Path, bridge_token: str, suffix: str, login: str,
                 uid: int | None = None, gid: int | None = None):
    text = render_preset(bridge_token, suffix, login)
    path = Path(path)
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    os.chmod(path, 0o600)
    if uid is not None and gid is not None:
        os.chown(path, uid, gid)


def authorized(line: str, login: str, server: str) -> bool:
    """True for MT5's ``'<login>': authorized on <server>`` log line."""
    return f"'{login}': authorized on {server}" in line


def redact(line: str) -> str:
    line = re.sub(r"(?i)(token|password|secret)=[^&\s]+", r"\1=<redacted>", line)
    line = re.sub(r"[A-Za-z0-9_-]{32,}", "<redacted>", line)
    return line.strip()[:300]

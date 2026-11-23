#!/usr/bin/env python3
"""Build a pristine, account-free MT5 template for the provisioner.

Run as root on the VPS, with the source terminal stopped:

    sudo /opt/guardrails/.venv/bin/python tools/create_mt5_template.py --source 61603648

The template keeps the MT5 binaries and the EA, but removes saved credentials,
per-account caches, logs, presets, and the one-time start.ini values. Every new
account is cloned from this tree by tools/linux_mt5_provision.py.
"""

from __future__ import annotations

import argparse
import grp
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path("/opt/guardrails")
MT5 = BASE / "mt5"
TEMPLATE = MT5 / "_template"

REMOVE_FILES = [
    "Config/accounts.dat",       # saved MT5 login/password
    "Config/terminal.ini",       # window/UI state
    "Config/common.ini",         # non-portable settings and environment hash
    "Config/hotkeys.ini",
    "Config/dnsperf.dat",
    "Config/assistant.ini",
    "MQL5/Presets/EA_Bridge.set",
]

REMOVE_DIRS = [
    "bases",
    "Tester",
    "llm-agent",
    "logs",
    "MQL5/Logs",
    "MQL5/Files/guardrails",
    "MQL5/Profiles",
    "Config/certificates",
]


def run(cmd: list[str], timeout: int = 300):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def guardrails_ids():
    return pwd.getpwnam("guardrails").pw_uid, grp.getgrnam("guardrails").gr_gid


def reset_dir(path: Path):
    if path.exists():
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
    else:
        path.mkdir(parents=True, exist_ok=True)


def write_template_start(uid: int, gid: int):
    sys.path.insert(0, str(BASE))
    import mt5_provisioning

    base = [
        "[Common]",
        "Login=",
        "Server=",
        "KeepPrivate=1",
        "[Experts]",
        "[StartUp]",
        "Symbol=EURUSD",
        "Period=H1",
    ]
    lines = mt5_provisioning.render_start_ini(
        base, login="", server="", password=None
    )
    mt5_provisioning.write_ini(TEMPLATE / "start.ini", lines, uid, gid)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="61603648", help="login tree to clone")
    parser.add_argument("--force", action="store_true",
                        help="copy even if the source terminal is active")
    args = parser.parse_args()

    if not hasattr(pwd, "getpwnam") or sys.platform != "linux":
        print("This script runs on the Linux VPS.", file=sys.stderr)
        return 2
    if subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip() != "0":
        print("Run this script as root.", file=sys.stderr)
        return 2

    source = MT5 / args.source
    if not (source / "terminal64.exe").is_file():
        print(f"Source terminal not found: {source}", file=sys.stderr)
        return 2
    if source == TEMPLATE:
        print("Source and template are the same directory.", file=sys.stderr)
        return 2

    active = subprocess.run(
        ["systemctl", "is-active", "--quiet", f"guardrails-mt5@{args.source}.service"]
    )
    if active.returncode == 0 and not args.force:
        print(
            f"Stop guardrails-mt5@{args.source}.service before copying; "
            "a running terminal writes files while they are cloned.",
            file=sys.stderr,
        )
        return 3

    if TEMPLATE.exists():
        print(f"Replacing existing template at {TEMPLATE}")
        shutil.rmtree(TEMPLATE)
    print(f"Cloning {source} -> {TEMPLATE}")
    result = run(["cp", "-a", str(source), str(TEMPLATE)], timeout=600)
    if result.returncode != 0:
        print(f"cp failed: {result.stderr.strip()}", file=sys.stderr)
        return 4

    for rel in REMOVE_FILES:
        path = TEMPLATE / rel
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    for rel in REMOVE_DIRS:
        reset_dir(TEMPLATE / rel)

    uid, gid = guardrails_ids()
    write_template_start(uid, gid)
    # Normalize permissions: owner-only access, keep execute bits on binaries.
    run(["chmod", "-R", "u+rwX,go-rwx", str(TEMPLATE)])
    run(["chown", "-R", f"{uid}:{gid}", str(TEMPLATE)])
    print("Template ready. Saved credentials, logs, presets, and broker caches removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

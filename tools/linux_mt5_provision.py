#!/usr/bin/env python3
"""Privileged MT5 first-login provisioner for the Linux VPS.

The backend queues an encrypted ``mt5_login`` job when the owner submits the
one-time setup form. This root-owned watcher claims jobs, clones a pristine
terminal tree, writes the password only for the first login, starts the
per-account systemd units, verifies the EA report, then scrubs the password.
Passwords and bridge tokens are never logged.
"""

from __future__ import annotations

import fcntl
import grp
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

BASE = Path("/opt/guardrails")
TEMPLATE = BASE / "mt5" / "_template"
BROKERS = BASE / "mt5" / "_brokers"
WINE = "/opt/wine-staging/bin/wine"
WINEPREFIX = BASE / "mt5" / "wineprefix-staging"
LOG = Path("/var/log/guardrails-mt5-provision.log")
LOCK = Path("/run/guardrails-mt5-provision.lock")
CONNECT_SECONDS = 420
REAUTH_SECONDS = 180
LOCAL_BACKEND = "http://127.0.0.1:8000"

sys.path.insert(0, str(BASE))


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
    path = BASE / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            key, value = raw.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def guardrails_ids():
    entry = pwd.getpwnam("guardrails")
    group = grp.getgrnam("guardrails")
    return entry.pw_uid, group.gr_gid


def run(cmd, timeout=60, env=None):
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
    except Exception as exc:  # noqa: BLE001
        log(f"command error: {' '.join(cmd)}: {type(exc).__name__}")
        return None
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip().replace("\n", " ")[:300]
        log(f"command rc={res.returncode}: {' '.join(cmd)} {detail}")
    return res


def systemctl(*args, timeout=90):
    return run(["systemctl", *args], timeout=timeout)


def latest_log(logs_dir: Path):
    if not logs_dir.exists():
        return "", None, 0
    files = sorted(
        (p for p in logs_dir.glob("*.log") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        return "", None, 0
    path = files[-1]
    try:
        text = path.read_bytes().decode("utf-16", errors="replace")
    except OSError:
        return "", path, 0
    return text, path, len(text.splitlines())


def new_lines(before_path, before_count: int, text: str, path):
    if path != before_path:
        return text.splitlines()
    return text.splitlines()[before_count:]


def account_report_ts(storage, login: str) -> float:
    account = storage.get_account(login)
    return float(account.get("last_report_ts") or 0) if account else 0.0


def chown_tree(path: Path):
    uid, gid = guardrails_ids()
    for root, dirs, files in os.walk(path):
        os.chown(root, uid, gid)
        for name in dirs + files:
            try:
                os.chown(Path(root) / name, uid, gid, follow_symlinks=False)
            except OSError:
                pass


def pack_for_server(server: str) -> Path | None:
    """Return the cached broker pack whose servers.txt lists this server."""
    if not BROKERS.is_dir():
        return None
    wanted = server.strip().lower()
    for pack in sorted(BROKERS.iterdir()):
        if not pack.is_dir():
            continue
        listing = pack / "servers.txt"
        if not listing.exists():
            continue
        try:
            names = {
                line.strip().lower()
                for line in listing.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
        except OSError:
            continue
        if wanted in names:
            return pack
        prefixes = pack / "prefixes.txt"
        if prefixes.exists():
            try:
                if any(
                    wanted.startswith(line.strip().lower())
                    for line in prefixes.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ):
                    return pack
            except OSError:
                pass
    return None


def apply_broker_pack(instance: Path, pack: Path):
    """Overlay a broker's branded terminal and server list on the template."""
    uid, gid = guardrails_ids()
    for rel in (
        "terminal64.exe",
        "Config/servers.dat",
        "Config/settings.ini",
        "Config/terminal.lic",
    ):
        source = pack / rel
        if not source.exists():
            continue
        target = instance / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        os.chown(target, uid, gid)
        os.chmod(target, 0o755 if rel.endswith(".exe") else 0o600)
    log(f"applied broker pack {pack.name}")


def install_broker_pack(url: str, server: str, mt5_provisioning) -> Path:
    """Download a broker installer and cache its branded terminal files.

    The installer runs under Wine as the unprivileged ``guardrails`` user. Only
    the terminal binary and its server/licence config are copied into the
    cache; the broker's MQL5 tree is never used.
    """
    url = mt5_provisioning.validate_broker_url(url)
    if not url:
        raise RuntimeError("broker download link is empty")
    existing = pack_for_server(server)
    if existing is not None:
        return existing

    slug = re.sub(r"[^a-z0-9]+", "-", server.lower()).strip("-") or "broker"
    pack = BROKERS / slug
    if pack.exists():
        # A pack directory already exists for this broker but did not list the
        # server yet (for example, a second server name on the same broker).
        with open(pack / "servers.txt", "a", encoding="utf-8") as fh:
            fh.write(server + "\n")
        chown_tree(pack)
        return pack

    BROKERS.mkdir(parents=True, exist_ok=True)
    installer = Path("/tmp") / f"guardrails-mt5-{slug}.exe"
    log(f"downloading broker installer for {server}")
    res = run(
        ["curl", "-fL", "--max-time", "300", "-o", str(installer), url],
        timeout=330,
    )
    if res is None or res.returncode != 0 or not installer.exists():
        raise RuntimeError("could not download the broker installer")
    if installer.stat().st_size > 250 * 1024 * 1024:
        installer.unlink(missing_ok=True)
        raise RuntimeError("broker installer is too large")

    programs = WINEPREFIX / "drive_c" / "Program Files"
    before = {p.name for p in programs.iterdir()} if programs.is_dir() else set()
    log(f"installing broker terminal for {server}")
    res = run(
        [
            "sudo", "-u", "guardrails", "env",
            f"HOME={BASE}", f"WINEPREFIX={WINEPREFIX}",
            "WINEDEBUG=-all", "WINEDLLOVERRIDES=mscoree,mshtml=",
            "DISPLAY=:1", WINE, str(installer), "/auto",
        ],
        timeout=600,
    )
    installer.unlink(missing_ok=True)
    if res is None or res.returncode != 0:
        raise RuntimeError("broker installer did not finish")

    if not programs.is_dir():
        raise RuntimeError("broker installer did not create a terminal")
    candidates = [
        p for p in programs.iterdir()
        if p.is_dir() and (p / "terminal64.exe").exists()
    ]
    fresh = [p for p in candidates if p.name not in before]
    source = max(fresh or candidates, key=lambda p: (p / "terminal64.exe").stat().st_mtime)
    pack.mkdir(parents=True, exist_ok=True)
    for rel in (
        "terminal64.exe",
        "Config/servers.dat",
        "Config/settings.ini",
        "Config/terminal.lic",
    ):
        src = source / rel
        if src.exists():
            dst = pack / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    (pack / "servers.txt").write_text(server + "\n", encoding="utf-8")
    chown_tree(pack)
    log(f"cached broker pack {pack.name} from {source.name}")
    return pack


def ensure_instance(login: str, server: str = "") -> Path:
    """Always start from a pristine template, never a failed half-provision."""
    instance = BASE / "mt5" / login
    if instance.exists():
        log(f"replacing existing terminal tree for {login}")
        shutil.rmtree(instance, ignore_errors=True)
    if not TEMPLATE.is_dir():
        raise RuntimeError(
            "pristine MT5 template is missing at /opt/guardrails/mt5/_template"
        )
    log(f"creating terminal tree for {login} from template")
    res = run(["cp", "-a", str(TEMPLATE), str(instance)], timeout=300)
    if res is None or res.returncode != 0:
        raise RuntimeError("could not clone the MT5 template")
    chown_tree(instance)
    os.chmod(instance, 0o700)
    if server:
        pack = pack_for_server(server)
        if pack is not None:
            apply_broker_pack(instance, pack)
    return instance


def clear_bridge_files(instance: Path):
    bridge_dir = instance / "MQL5" / "Files" / "guardrails"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    for child in bridge_dir.iterdir():
        if child.is_file():
            try:
                child.unlink()
            except OSError:
                pass


def prepare_files(*, instance: Path, login: str, server: str, password: str,
                  bridge_token: str, suffix: str, mt5_provisioning):
    uid, gid = guardrails_ids()
    start = instance / "start.ini"
    if start.exists():
        base_lines = mt5_provisioning.read_ini(start)
    else:
        base_lines = ["[Common]", "", "[Experts]", "", "[StartUp]", ""]
    lines = mt5_provisioning.render_start_ini(
        base_lines, login=login, server=server, password=password
    )
    mt5_provisioning.write_ini(start, lines, uid, gid)

    preset = instance / "MQL5" / "Presets" / "EA_Bridge.set"
    preset.parent.mkdir(parents=True, exist_ok=True)
    mt5_provisioning.write_preset(
        preset, bridge_token=bridge_token, suffix=suffix, login=login,
        uid=uid, gid=gid,
    )
    clear_bridge_files(instance)
    os.chown(instance, uid, gid)


def scrub(instance: Path, mt5_provisioning):
    start = instance / "start.ini"
    if not start.exists():
        return
    uid, gid = guardrails_ids()
    lines = mt5_provisioning.read_ini(start)
    cleaned = mt5_provisioning.scrub_password_lines(lines)
    mt5_provisioning.write_ini(start, cleaned, uid, gid)


def wait_for_login(*, storage, instance: Path, login: str, server: str,
                   since: float, require_report: bool, timeout: int,
                   mt5_provisioning):
    terminal_before, terminal_path, terminal_count = latest_log(instance / "logs")
    ea_before, ea_path, ea_count = latest_log(instance / "MQL5" / "Logs")
    auth = False
    ea_loaded = False
    report = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        text, path, _ = latest_log(instance / "logs")
        for line in new_lines(terminal_path, terminal_count, text, path):
            if mt5_provisioning.authorized(line, login, server):
                auth = True
        ea_text, ea_path_now, _ = latest_log(instance / "MQL5" / "Logs")
        if not ea_loaded:
            for line in new_lines(ea_path, ea_count, ea_text, ea_path_now):
                if "EA_Bridge" in line and (
                    "loaded" in line or "initialized" in line
                ):
                    ea_loaded = True
        if account_report_ts(storage, login) >= since - 5:
            report = True
        # An EA report is proof of a live authorized session. MT5 buffers the
        # terminal log file, so the "authorized on <server>" line can be
        # missing for minutes even though login succeeded.
        if report or (auth and not require_report):
            break
        time.sleep(5)
    return auth, ea_loaded, report


def start_units(login: str):
    systemctl("start", "guardrails-xvfb")
    systemctl("enable", f"guardrails-mt5@{login}.service",
              f"guardrails-mt5-bridge@{login}.service")
    res = systemctl("restart", f"guardrails-mt5@{login}.service")
    if res is None or res.returncode != 0:
        raise RuntimeError("could not start the MT5 terminal unit")
    systemctl("restart", f"guardrails-mt5-bridge@{login}.service")


def stop_units(login: str):
    systemctl("stop", f"guardrails-mt5-bridge@{login}.service",
              f"guardrails-mt5@{login}.service")


def provision_login(*, job, storage, provisioning, mt5_provisioning):
    login = mt5_provisioning.validate_login(str(job.get("login") or ""))
    account = storage.get_account(login)
    if not account:
        raise RuntimeError("account is not registered")
    if (account.get("platform") or "mt5") != "mt5":
        raise RuntimeError("not an MT5 account")
    if account.get("phase") not in ("challenge", "funded"):
        raise RuntimeError("account has no phase")

    payload = provisioning.decrypt_json(job.get("payload_enc") or "")
    password = mt5_provisioning.validate_password(str(payload.get("password") or ""))
    server = mt5_provisioning.validate_server(
        str(payload.get("server") or account.get("server") or "")
    )
    bridge_token = str(payload.get("bridge_token") or "")
    if not bridge_token:
        raise RuntimeError("setup payload is missing the bridge token")
    suffix = str(payload.get("suffix") or account.get("suffix") or "0")
    broker_url = str(payload.get("broker_url") or "")

    storage.update_account_provision(
        login, server=server, instance_status="provisioning",
        instance_dir=str(BASE / "mt5" / login), provision_error="",
        bridge_token_hash=provisioning.hash_bridge_token(bridge_token),
    )
    stop_units(login)
    if broker_url and pack_for_server(server) is None:
        install_broker_pack(broker_url, server, mt5_provisioning)
    instance = ensure_instance(login, server)
    prepare_files(
        instance=instance, login=login, server=server, password=password,
        bridge_token=bridge_token, suffix=suffix,
        mt5_provisioning=mt5_provisioning,
    )

    started_at = time.time()
    start_units(login)
    auth, ea_loaded, report = wait_for_login(
        storage=storage, instance=instance, login=login, server=server,
        since=started_at, require_report=True, timeout=CONNECT_SECONDS,
        mt5_provisioning=mt5_provisioning,
    )
    log(
        f"{login}: first login auth={int(auth)} ea={int(ea_loaded)} "
        f"report={int(report)}"
    )
    if not report:
        raise RuntimeError("MT5 did not authorize and report in time")

    scrub(instance, mt5_provisioning)
    restarted_at = time.time()
    res = systemctl("restart", f"guardrails-mt5@{login}.service")
    if res is None or res.returncode != 0:
        raise RuntimeError("could not restart MT5 after removing the password")
    auth2, ea2, report2 = wait_for_login(
        storage=storage, instance=instance, login=login, server=server,
        since=restarted_at, require_report=True, timeout=REAUTH_SECONDS,
        mt5_provisioning=mt5_provisioning,
    )
    log(
        f"{login}: saved-login auth={int(auth2)} ea={int(ea2)} "
        f"report={int(report2)}"
    )
    if not report2:
        raise RuntimeError("MT5 did not log in again from the saved account")

    storage.update_account_provision(
        login, instance_status="running", instance_dir=str(instance),
        provision_error="",
    )
    return "terminal running and reporting"


def remove_login(*, job):
    login = str(job.get("login") or "").strip()
    if not re.match(r"^\d{4,12}$", login):
        raise RuntimeError("invalid login in removal job")
    stop_units(login)
    systemctl("disable", f"guardrails-mt5@{login}.service",
              f"guardrails-mt5-bridge@{login}.service")
    return "terminal stopped"


def process_job(*, job, storage, provisioning, mt5_provisioning):
    action = job.get("action")
    login = str(job.get("login") or "")
    try:
        if action == "mt5_login":
            result = provision_login(
                job=job, storage=storage, provisioning=provisioning,
                mt5_provisioning=mt5_provisioning,
            )
            storage.complete_provision_job(job["id"], "done", result=result)
            storage.enqueue_notification(
                f"**MT5 `{login}` is live.** The terminal is running and "
                "reporting to GuardRails."
            )
            log(f"{login}: provisioning completed")
        elif action == "mt5_remove":
            result = remove_login(job=job)
            storage.complete_provision_job(job["id"], "done", result=result)
            storage.enqueue_notification(
                f"**MT5 `{login}` terminal stopped.** Its files remain on the "
                "VPS; the account is no longer registered."
            )
            log(f"{login}: removal completed")
        else:
            storage.complete_provision_job(
                job["id"], "error", error=f"unknown action {action}"
            )
    except Exception as exc:  # noqa: BLE001
        message = str(exc)[:300] or type(exc).__name__
        log(f"{login}: provisioning failed: {message}")
        if action == "mt5_login":
            try:
                stop_units(login)
            except Exception:
                pass
            instance = BASE / "mt5" / login
            try:
                scrub(instance, mt5_provisioning)
            except Exception:
                log(f"{login}: password scrub failed; manual check required")
            storage.update_account_provision(
                login, instance_status="error", provision_error=message,
            )
        storage.complete_provision_job(job["id"], "error", error=message)
        storage.enqueue_notification(
            f"**MT5 `{login}` setup failed:** {message}. Run `/addaccount` "
            "again to retry."
        )
        log(traceback.format_exc(limit=2).replace("\n", " | "))


def main() -> int:
    lock_fd = open(LOCK, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another provisioner is already running")
        return 0

    load_env()
    os.chdir(BASE)
    import mt5_provisioning  # noqa: E402
    import provisioning  # noqa: E402
    import storage  # noqa: E402

    stale = storage.reset_stale_provision_jobs()
    if stale:
        log(f"returned {stale} stale job(s) to the queue")
    log("provisioner started")
    while True:
        job = storage.claim_next_provision_job()
        if not job:
            time.sleep(5)
            continue
        process_job(
            job=job, storage=storage, provisioning=provisioning,
            mt5_provisioning=mt5_provisioning,
        )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

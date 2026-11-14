"""
GuardRails Windows provisioning agent.

Runs on the Windows MT5 host, polls the backend outbound (no inbound ports),
creates one portable MT5 instance per account, logs in once with credentials
delivered through the encrypted provisioning job, attaches the EA, supervises
the process, and reports status back.

The agent never persists the MT5 password. The initial start.ini contains the
password only until MT5 confirms authorization, then it is rewritten without it.
MT5 keeps the encrypted account database for subsequent auto-logins.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

VERSION = "1.0.5"
AGENT_ID_DEFAULT = "windows-1"

META_EQ_TEMPLATE = "MetaEq"
META_EQ_STRATEGIES = ("S1", "S2A", "S2B", "S3", "S3B", "S4", "S5", "S6", "S7", "S8", "S9")
META_EQ_PAIR_ROOTS = ("NAS100", "XAUUSD", "USDJPY")

CONFIG_PATH = Path(__file__).with_name("config.json")
LOG_PATH = Path(__file__).with_name("agent.log")

DEFAULT_CONFIG = {
    "backend_url": "",
    "agent_token": "",
    "agent_id": AGENT_ID_DEFAULT,
    "instance_root": r"C:\GuardRails\instances",
    "template_dir": r"C:\GuardRails\mt5-template",
    "server_lists_dir": r"C:\GuardRails\server-lists",
    "poll_seconds": 3,
    "heartbeat_seconds": 20,
    "start_timeout_seconds": 120,
    "stop_timeout_seconds": 30,
    "max_restarts": 5,
    "preset_name": "EA_Bridge",
    "expert_name": "EA_Bridge",
}

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102

log = logging.getLogger("guardrails-agent")


# --------------------------------------------------------------------------
# Configuration / logging
# --------------------------------------------------------------------------


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("r", encoding="utf-8-sig") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise SystemExit(f"{CONFIG_PATH} must contain a JSON object")
        config.update(loaded)
    env_map = {
        "GUARDRAILS_BACKEND_URL": "backend_url",
        "GUARDRAILS_AGENT_TOKEN": "agent_token",
        "GUARDRAILS_AGENT_ID": "agent_id",
        "GUARDRAILS_INSTANCE_ROOT": "instance_root",
        "GUARDRAILS_TEMPLATE_DIR": "template_dir",
        "GUARDRAILS_SERVER_LISTS_DIR": "server_lists_dir",
    }
    for env_key, config_key in env_map.items():
        if os.environ.get(env_key):
            config[config_key] = os.environ[env_key]
    config["backend_url"] = str(config["backend_url"]).rstrip("/")
    config["agent_token"] = str(config["agent_token"])
    if not config["backend_url"]:
        raise SystemExit("backend_url is missing from config.json")
    if not config["agent_token"]:
        raise SystemExit("agent_token is missing from config.json")
    return config


def broker_key(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def apply_server_list(dest: Path, server: str, config: dict) -> Path | None:
    """Copy the broker's servers.dat into a new instance.

    MT5 only knows the access points stored in servers.dat. A template built
    from one broker's terminal cannot log in to another broker until that
    broker's file is provided. Server lists live in ``server_lists_dir`` and
    are named after the server prefix, for example ``ftmo.dat`` or
    ``fundingpips.dat``. The longest matching prefix wins. When nothing
    matches, the template's own list is kept.
    """
    directory = Path(str(config.get("server_lists_dir") or ""))
    server_key = broker_key(server)
    if not directory.is_dir() or not server_key:
        return None
    best: tuple[int, Path] | None = None
    for path in directory.glob("*.dat"):
        key = broker_key(path.stem)
        if not key:
            continue
        if server_key.startswith(key) or key.startswith(server_key):
            if best is None or len(key) > best[0]:
                best = (len(key), path)
    if best is None:
        log.warning("no broker server list matched %r in %s; using the template default", server, directory)
        return None
    target = dest / "config" / "servers.dat"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best[1], target)
    log.info("using broker server list %s for %r", best[1].name, server)
    return best[1]


def setup_logging():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(console)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class AgentHTTPError(RuntimeError):
    pass


class Agent:
    def __init__(self, config: dict):
        self.config = config
        self.backend_url = config["backend_url"]
        self.agent_token = config["agent_token"]
        self.agent_id = str(config["agent_id"])[:64]
        self.instance_root = Path(config["instance_root"])
        self.template_dir = Path(config["template_dir"])
        self.state_path = self.instance_root / "agent_state.json"
        self.state = self._load_state()
        self.last_heartbeat = 0.0
        self.last_connect_error = 0.0
        self.ssl_context = ssl.create_default_context()

    # ---------------------------- state ---------------------------------

    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                with self.state_path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict) and isinstance(data.get("instances"), dict):
                    data.setdefault("pending_results", [])
                    return data
            except (OSError, ValueError):
                pass
        return {"instances": {}, "pending_results": []}

    def save_state(self):
        self.instance_root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.state_path)

    def instances(self) -> dict:
        return self.state.setdefault("instances", {})

    # ---------------------------- HTTP ----------------------------------

    def api(self, method: str, path: str, payload=None, timeout: int = 30):
        url = f"{self.backend_url}{path}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("x-agent-token", self.agent_token)
        request.add_header("x-agent-id", self.agent_id)
        request.add_header("ngrok-skip-browser-warning", "true")
        request.add_header("User-Agent", f"GuardRails-Agent/{VERSION}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.ssl_context) as response:
                body = response.read()
                if not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AgentHTTPError(f"backend returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AgentHTTPError(f"backend unreachable: {exc}") from exc
        except ValueError as exc:
            raise AgentHTTPError("backend returned invalid JSON") from exc

    # ---------------------------- heartbeat -----------------------------

    def heartbeat(self, force: bool = False):
        now = time.time()
        if not force and now - self.last_heartbeat < float(self.config["heartbeat_seconds"]):
            return
        instances = self.instances()
        running = sum(1 for item in instances.values() if item.get("status") == "running")
        free_mb, total_mb = memory_mb()
        body = {
            "agent_id": self.agent_id,
            "hostname": socket.gethostname(),
            "running": running,
            "total": len(instances),
            "ram_free_mb": free_mb,
            "ram_total_mb": total_mb,
            "version": VERSION,
            "instances": [
                {
                    "login": login,
                    "status": item.get("status") or "unknown",
                    "pid": int(item.get("pid") or 0),
                    "dir": item.get("dir") or "",
                }
                for login, item in instances.items()
            ],
        }
        try:
            self.api("POST", "/agent/heartbeat", body)
            self.last_heartbeat = now
            self.last_connect_error = 0
        except AgentHTTPError as exc:
            if now - self.last_connect_error > 60:
                log.warning("heartbeat failed: %s", exc)
                self.last_connect_error = now

    # ---------------------------- results -------------------------------

    def queue_result(self, job_id: int, status: str, result=None, error: str = ""):
        self.state.setdefault("pending_results", []).append({
            "job_id": int(job_id),
            "status": status,
            "result": result if result is not None else {},
            "error": (error or "")[:1000],
            "queued_ts": time.time(),
        })
        self.save_state()

    def flush_results(self):
        pending = self.state.setdefault("pending_results", [])
        remaining = []
        for item in pending:
            try:
                self.api(
                    "POST",
                    f"/agent/jobs/{item['job_id']}/result",
                    {"status": item["status"], "result": item.get("result") or {}, "error": item.get("error") or ""},
                    timeout=20,
                )
                log.info("reported result for job %s: %s", item["job_id"], item["status"])
            except AgentHTTPError as exc:
                message = str(exc)
                if "HTTP 404" in message or "HTTP 409" in message:
                    log.info("job %s result already recorded; dropping local copy", item["job_id"])
                    continue
                if time.time() - float(item.get("queued_ts") or 0) < 3600:
                    remaining.append(item)
                else:
                    log.error("giving up reporting job %s after 1h: %s", item["job_id"], message)
        if len(remaining) != len(pending):
            self.state["pending_results"] = remaining
            self.save_state()

    # ---------------------------- jobs ----------------------------------

    def poll_jobs(self):
        try:
            response = self.api("GET", "/agent/jobs", timeout=20)
        except AgentHTTPError as exc:
            now = time.time()
            if now - self.last_connect_error > 60:
                log.warning("job poll failed: %s", exc)
                self.last_connect_error = now
            return
        job = response.get("job") if isinstance(response, dict) else None
        if not job:
            return
        job_id = int(job.get("id") or 0)
        action = str(job.get("action") or "")
        login = str(job.get("login") or "")
        payload = job.get("payload") or {}
        if not job_id or not action or not login:
            return
        log.info("job %s: %s for %s", job_id, action, login)
        self.heartbeat(force=True)
        try:
            if action == "provision":
                result = self.provision(login, payload)
            elif action == "stop":
                result = self.stop(login)
            elif action == "restart":
                result = self.restart(login, payload)
            elif action == "remove":
                result = self.remove(login)
            elif action == "metaeq_apply":
                result = self.metaeq_apply(login, payload)
            elif action == "metaeq_detach":
                result = self.metaeq_detach(login)
            else:
                raise RuntimeError(f"unknown action: {action}")
            self.queue_result(job_id, "done", result)
        except Exception as exc:
            message = sanitize_error(str(exc))
            log.error("job %s failed: %s", job_id, message)
            self.queue_result(job_id, "failed", {}, message)
        self.flush_results()

    # ---------------------------- lifecycle -----------------------------

    def instance_dir(self, login: str) -> Path:
        if not login.isdigit():
            raise RuntimeError("login is not numeric")
        return self.instance_root / login

    def template_exe(self) -> Path:
        exe = self.template_dir / "terminal64.exe"
        if not exe.exists():
            raise RuntimeError(f"portable MT5 template not found at {self.template_dir}")
        return exe

    def provision(self, login: str, payload: dict) -> dict:
        password = str(payload.get("password") or "")
        server = str(payload.get("server") or "").strip()
        suffix = str(payload.get("suffix") or "0").strip() or "0"
        bridge_token = str(payload.get("bridge_token") or "")
        backend_url = str(payload.get("backend_url") or self.backend_url).rstrip("/")
        symbols = payload.get("symbols") or []
        if not login.isdigit():
            raise RuntimeError("login is not numeric")
        if not password:
            raise RuntimeError("provisioning payload has no password")
        if not server:
            raise RuntimeError("provisioning payload has no server")
        if not bridge_token:
            raise RuntimeError("provisioning payload has no bridge token")
        self.template_exe()

        dest = self.instance_dir(login)
        existing = self.instances().get(login)
        if existing and existing.get("status") == "running" and pid_alive(int(existing.get("pid") or 0)):
            raise RuntimeError("an instance for this login is already running")

        self.stop(login, quiet=True)
        if dest.exists():
            remove_tree(dest)
        log.info("copying MT5 template to %s", dest)
        shutil.copytree(self.template_dir, dest)
        server_list = apply_server_list(dest, server, self.config)
        self.heartbeat(force=True)

        config_dir = dest / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (dest / "MQL5" / "Presets").mkdir(parents=True, exist_ok=True)

        first_symbol = ""
        for symbol in symbols:
            text = str(symbol or "").strip()
            if text:
                first_symbol = text
                break
        if not first_symbol:
            first_symbol = "EURUSD"
        if suffix and suffix != "0":
            first_symbol += suffix

        preset_path = dest / "MQL5" / "Presets" / f"{self.config['preset_name']}.set"
        write_preset(preset_path, backend_url, bridge_token, login, suffix)

        common_path = config_dir / "common.ini"
        start_path = dest / "start.ini"
        write_start_ini(common_path, start_path, login, server, password, first_symbol, self.config["preset_name"])

        pid = 0
        active_pid = 0
        baseline = current_log_text(dest)
        try:
            pid = launch_terminal(dest, start_path)
            ok, detail, active_pid = wait_for_login(
                dest, login, pid, float(self.config["start_timeout_seconds"]), self.heartbeat, baseline
            )
            if not ok:
                if detail == "timed out waiting for MT5 to confirm the login" and server_list is None:
                    detail += (
                        f"; no broker server list matched {server!r}, so MT5 may not know that server"
                    )
                raise RuntimeError(detail)
            pid = active_pid or pid
            # Keep a passwordless start.ini so every restart re-attaches the EA
            # and uses MT5's encrypted saved credentials.
            write_start_ini(common_path, start_path, login, server, "", first_symbol, self.config["preset_name"])
        except Exception:
            kill_instance_processes(dest)
            try:
                start_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        finally:
            # The initial config is the only place the plaintext password ever
            # touches disk. If rewriting succeeded it is already gone.
            try:
                if start_path.exists() and "Password=" in read_text(start_path):
                    start_path.unlink(missing_ok=True)
            except OSError:
                pass

        item = self.instances().setdefault(login, {})
        item.update({
            "login": login,
            "status": "running",
            "pid": pid,
            "dir": str(dest),
            "server": server,
            "suffix": suffix,
            "restarts": 0,
            "next_restart": 0,
            "last_error": "",
            "provisioned_at": time.time(),
        })
        self.save_state()
        self.heartbeat(force=True)
        log.info("instance %s is running (pid %s)", login, pid)
        return {"instance_dir": str(dest), "pid": pid, "login_confirmed": True}

    def start_existing(self, login: str, wait: bool = True) -> dict:
        item = self.instances().get(login)
        if not item:
            raise RuntimeError("no provisioned instance for this login")
        dest = Path(item.get("dir") or self.instance_dir(login))
        exe = dest / "terminal64.exe"
        if not exe.exists():
            raise RuntimeError("instance directory is missing terminal64.exe")
        start_path = dest / "start.ini"
        if not start_path.exists():
            raise RuntimeError("start.ini is missing; re-provision the account")
        baseline = current_log_text(dest)
        pid = launch_terminal(dest, start_path)
        item["pid"] = pid
        item["status"] = "starting"
        item["last_error"] = ""
        self.save_state()
        if wait:
            ok, detail, active_pid = wait_for_login(
                dest, login, pid, float(self.config["start_timeout_seconds"]), self.heartbeat, baseline
            )
            if not ok:
                kill_instance_processes(dest)
                item["status"] = "error"
                item["last_error"] = sanitize_error(detail)
                self.save_state()
                raise RuntimeError(detail)
            pid = active_pid or pid
        item["pid"] = pid
        item["status"] = "running"
        item["last_error"] = ""
        self.save_state()
        self.heartbeat(force=True)
        return {"instance_dir": str(dest), "pid": pid}

    def stop(self, login: str, quiet: bool = False) -> dict:
        item = self.instances().get(login) or {}
        pid = int(item.get("pid") or 0)
        directory = Path(item.get("dir") or self.instance_dir(login))
        found = pids_for_dir(directory)
        if pid and pid_alive(pid) and pid not in found:
            found.insert(0, pid)
        for process_id in found:
            kill_pid(process_id, timeout=float(self.config["stop_timeout_seconds"]))
        if item:
            item["status"] = "stopped"
            item["pid"] = 0
            item["next_restart"] = 0
            self.save_state()
        if not quiet:
            log.info("instance %s stopped", login)
        return {"stopped": bool(found), "pids": found}

    def restart(self, login: str, payload: dict) -> dict:
        self.stop(login)
        time.sleep(2)
        return self.start_existing(login)

    def remove(self, login: str) -> dict:
        self.stop(login)
        dest = self.instance_dir(login)
        time.sleep(1)
        if dest.exists():
            remove_tree(dest)
        self.instances().pop(login, None)
        self.save_state()
        log.info("instance %s removed", login)
        return {"removed": not dest.exists()}

    # ---------------------------- MetaEq manager -------------------------

    def metaeq_apply(self, login: str, payload: dict) -> dict:
        dest = self.instance_dir(login)
        if not dest.exists():
            raise RuntimeError("instance directory is missing; provision the account first")
        if not (dest / "terminal64.exe").exists():
            raise RuntimeError("instance directory has no terminal64.exe")

        suffix = str(payload.get("suffix") or "0").strip() or "0"
        raw_symbols = payload.get("symbols") or {}
        if not isinstance(raw_symbols, dict):
            raise RuntimeError("MetaEq payload has no symbol map")
        symbols = {}
        for pair in META_EQ_PAIR_ROOTS:
            value = str(raw_symbols.get(pair) or "").strip()
            if not value:
                value = pair if suffix == "0" else pair + suffix
            symbols[pair] = value
        chart_symbol = str(payload.get("chart_symbol") or symbols["NAS100"]).strip()

        control = str(payload.get("control") or "")
        if not control.strip():
            raise RuntimeError("MetaEq payload has no control state")

        experts = dest / "MQL5" / "Experts"
        presets = dest / "MQL5" / "Presets"
        templates = dest / "MQL5" / "Profiles" / "Templates"
        meta_dir = dest / "MQL5" / "Files" / "MetaEq"
        guard_dir = dest / "MQL5" / "Files" / "GuardRails"
        for directory in (experts, presets, templates, meta_dir, guard_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self._install_metaeq_files(experts, presets)
        preset_path = presets / "MetaEq.set"
        values = read_set_values(preset_path)
        template_text = render_metaeq_template(values, symbols, chart_symbol)
        revision = hashlib.sha1(template_text.encode("utf-8")).hexdigest()[:16]
        write_utf16_atomic(templates / "MetaEq.tpl", template_text)

        # Write control before asking EA_Bridge to open the chart. MetaEq
        # fails closed, so the order only affects the first second of life.
        write_text_atomic(meta_dir / "control.ini", control)
        ini = (
            "; GuardRails MetaEq supervisor. Generated by the Windows agent.\r\n"
            "active=1\r\n"
            f"template=MetaEq.tpl\r\n"
            f"symbol={chart_symbol}\r\n"
            "period=16385\r\n"
            f"revision={revision}\r\n"
        )
        write_text_atomic(guard_dir / "metaeq.ini", ini)
        log.info("MetaEq applied to %s (chart %s, revision %s)", login, chart_symbol, revision)
        return {
            "attached": True,
            "symbol": chart_symbol,
            "revision": revision,
            "control": str(meta_dir / "control.ini"),
        }

    def metaeq_detach(self, login: str) -> dict:
        dest = self.instance_dir(login)
        if not dest.exists():
            return {"attached": False, "already_absent": True}
        guard_dir = dest / "MQL5" / "Files" / "GuardRails"
        meta_dir = dest / "MQL5" / "Files" / "MetaEq"
        guard_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(guard_dir / "metaeq.ini",
                          "; GuardRails MetaEq supervisor.\r\n"
                          "active=0\r\n"
                          "template=MetaEq.tpl\r\n")
        control_path = meta_dir / "control.ini"
        if control_path.exists():
            try:
                control_path.unlink()
            except OSError as exc:
                log.warning("could not remove %s: %s", control_path, sanitize_error(str(exc)))
        log.info("MetaEq detached from %s", login)
        return {"attached": False}

    def _install_metaeq_files(self, experts: Path, presets: Path):
        ex5_src = self.template_dir / "MQL5" / "Experts" / "MetaEq.ex5"
        ex5_dst = experts / "MetaEq.ex5"
        if ex5_src.exists():
            try:
                shutil.copy2(ex5_src, ex5_dst)
            except OSError as exc:
                if not ex5_dst.exists():
                    raise
                log.warning(
                    "MetaEq.ex5 is in use; keeping the installed copy: %s",
                    sanitize_error(str(exc)),
                )
        elif not ex5_dst.exists():
            raise RuntimeError("MetaEq.ex5 is missing from the template and the instance")

        preset_src = self.template_dir / "MQL5" / "Presets" / "MetaEq.set"
        preset_dst = presets / "MetaEq.set"
        if preset_src.exists():
            try:
                shutil.copy2(preset_src, preset_dst)
            except OSError as exc:
                if not preset_dst.exists():
                    raise
                log.warning(
                    "MetaEq.set is in use; keeping the installed copy: %s",
                    sanitize_error(str(exc)),
                )
        elif not preset_dst.exists():
            repo_preset = Path(__file__).resolve().parent.parent / "MetaEq.set"
            if repo_preset.exists():
                shutil.copy2(repo_preset, preset_dst)
            else:
                raise RuntimeError("MetaEq.set is missing from the template and the instance")

    def supervise(self):
        now = time.time()
        changed = False
        max_restarts = int(self.config.get("max_restarts") or 5)
        for login, item in list(self.instances().items()):
            status = item.get("status") or ""
            if status == "running":
                pid = int(item.get("pid") or 0)
                instance_pids = pids_for_dir(Path(item.get("dir") or self.instance_dir(login)))
                if pid_alive(pid) or instance_pids:
                    if instance_pids and pid not in instance_pids:
                        item["pid"] = instance_pids[0]
                        changed = True
                    continue
                log.warning("instance %s exited unexpectedly (pid %s)", login, pid)
                item["status"] = "restarting"
                item["last_error"] = "process exited unexpectedly"
                item["restarts"] = int(item.get("restarts") or 0) + 1
                item["next_restart"] = now
                changed = True

            if item.get("status") not in ("restarting", "error"):
                continue
            next_restart = float(item.get("next_restart") or 0)
            if not next_restart or now < next_restart:
                continue
            restarts = int(item.get("restarts") or 0)
            if restarts > max_restarts:
                item["status"] = "error"
                item["last_error"] = f"crashed {restarts} times; restart limit reached"
                item["next_restart"] = 0
                changed = True
                log.error("instance %s reached restart limit", login)
                continue
            try:
                self.start_existing(login, wait=True)
                item["restarts"] = 0
                item["next_restart"] = 0
                item["status"] = "running"
                item["last_error"] = ""
            except Exception as exc:
                item["status"] = "error"
                item["last_error"] = sanitize_error(str(exc))
                item["next_restart"] = time.time() + backoff_seconds(restarts)
                log.error("restart of %s failed: %s", login, item["last_error"])
            changed = True
        if changed:
            self.save_state()
            self.heartbeat(force=True)

    # ---------------------------- main loop -----------------------------

    def run(self):
        self.instance_root.mkdir(parents=True, exist_ok=True)
        log.info("GuardRails agent %s starting as %s", VERSION, self.agent_id)
        log.info("backend %s; template %s", self.backend_url, self.template_dir)
        self.scrub_password_files()
        self.heartbeat(force=True)
        while True:
            try:
                self.heartbeat()
                self.flush_results()
                self.poll_jobs()
                self.supervise()
            except Exception as exc:
                log.exception("agent loop error: %s", sanitize_error(str(exc)))
            time.sleep(max(1.0, float(self.config["poll_seconds"])))

    def scrub_password_files(self):
        """Remove leftover plaintext passwords if the agent crashed mid-launch."""
        if not self.instance_root.exists():
            return
        for directory in self.instance_root.iterdir():
            if not directory.is_dir():
                continue
            start_path = directory / "start.ini"
            common_path = directory / "config" / "common.ini"
            if not start_path.exists() or not common_path.exists():
                continue
            try:
                if "Password=" not in read_text(start_path):
                    continue
                sections = parse_ini(read_text(start_path))
                common = {key.lower(): value for key, value in sections.get("Common", [])}
                startup = {key.lower(): value for key, value in sections.get("StartUp", [])}
                login = common.get("login", "")
                server = common.get("server", "")
                symbol = startup.get("symbol", "EURUSD")
                if login and server:
                    write_start_ini(common_path, start_path, login, server, "", symbol,
                                    str(self.config.get("preset_name") or "EA_Bridge"))
                    log.warning("scrubbed leftover password from %s", start_path)
            except (OSError, ValueError) as exc:
                log.warning("could not scrub %s: %s", start_path, sanitize_error(str(exc)))


# --------------------------------------------------------------------------
# MT5 config files
# --------------------------------------------------------------------------


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8")
    return raw.decode("utf-8", errors="replace")


def parse_ini(text: str) -> OrderedDict:
    sections: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current is not None and "=" in line:
            key, value = line.split("=", 1)
            sections[current].append((key.strip(), value.strip()))
    return sections


def set_key(sections: dict, section: str, key: str, value: str):
    rows = sections.setdefault(section, [])
    rows[:] = [(k, v) for k, v in rows if k.lower() != key.lower()]
    rows.append((key, value))


def write_start_ini(common_path: Path, start_path: Path, login: str, server: str,
                    password: str, symbol: str, preset_name: str):
    sections = parse_ini(read_text(common_path)) if common_path.exists() else OrderedDict()
    # A custom /config: file supplies all platform parameters, so include the
    # copied WebRequest rule and other common.ini sections in it.
    for name in list(sections):
        sections[name] = [
            (key, value) for key, value in sections[name]
            if key.lower() not in {"password", "certpassword", "proxypassword", "mql5password"}
        ]
    set_key(sections, "Common", "Login", login)
    set_key(sections, "Common", "Server", server)
    set_key(sections, "Common", "KeepPrivate", "1")
    if password:
        set_key(sections, "Common", "Password", password)
    set_key(sections, "Experts", "AllowLiveTrading", "1")
    set_key(sections, "Experts", "AllowDllImport", "0")
    set_key(sections, "Experts", "Enabled", "1")
    set_key(sections, "Experts", "Account", "0")
    set_key(sections, "Experts", "Profile", "0")
    set_key(sections, "Experts", "WebRequest", "1")
    set_key(sections, "StartUp", "Expert", f"{preset_name}")
    set_key(sections, "StartUp", "ExpertParameters", f"{preset_name}.set")
    set_key(sections, "StartUp", "Symbol", symbol)
    set_key(sections, "StartUp", "Period", "H1")

    order = ["Common", "Charts", "Experts", "StartUp", "Notification", "Trades", "Objects"]
    lines = []
    for name in order:
        if name in sections:
            lines.append(f"[{name}]")
            lines.extend(f"{key}={value}" for key, value in sections[name])
            lines.append("")
    for name, rows in sections.items():
        if name in order:
            continue
        lines.append(f"[{name}]")
        lines.extend(f"{key}={value}" for key, value in rows)
        lines.append("")
    text = "\r\n".join(lines).rstrip() + "\r\n"
    start_path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))


def write_preset(path: Path, backend_url: str, bridge_token: str, login: str, suffix: str):
    values = [
        ("BackendURL", backend_url),
        ("AccountToken", bridge_token),
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
    text = "\r\n".join(f"{key}={value}" for key, value in values) + "\r\n"
    path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))


# --------------------------------------------------------------------------
# MetaEq files
# --------------------------------------------------------------------------


def read_set_values(path: Path) -> OrderedDict:
    """Read an MT5 .set file, dropping the tester's ||min||step||max||flag tail."""
    values: OrderedDict[str, str] = OrderedDict()
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if "||" in value:
            value = value.split("||", 1)[0]
        values[key.strip()] = value.strip()
    return values


def _metaeq_digits(symbol: str) -> int:
    upper = symbol.upper()
    if "JPY" in upper:
        return 3
    if "XAU" in upper:
        return 2
    if "NAS" in upper or "US100" in upper or "USTEC" in upper:
        return 1
    return 5


def render_metaeq_template(values: OrderedDict, symbols: dict, chart_symbol: str) -> str:
    """Turn MetaEq.set into a chart template with the account's exact symbols."""
    allocator = {
        "S1_Symbols": symbols["NAS100"],
        "S2A_Symbols": symbols["USDJPY"],
        "S2B_Symbols": symbols["XAUUSD"],
        "S3_Symbols": symbols["NAS100"],
        "S3B_Symbols": symbols["USDJPY"],
        "S4_Symbol": "CURRENT",
        "S5_Symbol": symbols["USDJPY"],
        "S6_Symbol": symbols["XAUUSD"],
        "S7_Symbol": symbols["USDJPY"],
        "S8_Symbol": symbols["NAS100"],
        "S9_Symbols": symbols["NAS100"],
    }
    overrides = {
        "GlobalBaseMagic": "100000",
        "GlobalComment": "MetaEq",
        **allocator,
    }
    # Keep every strategy initialized so a runtime gate can switch entries on
    # later without restarting the terminal.
    for name in META_EQ_STRATEGIES:
        overrides[f"{name}_Enabled"] = "true"
    for key, value in overrides.items():
        values[key] = value

    lines = [
        "<chart>",
        "id=0",
        f"symbol={chart_symbol}",
        "period_type=1",
        "period_size=1",
        f"digits={_metaeq_digits(chart_symbol)}",
        "tick_size=0.000000",
        "position_time=0",
        "scale_fix=0",
        "scale_fixed_min=0.000000",
        "scale_fixed_max=0.000000",
        "scale_fix11=0",
        "scale_bar=0",
        "scale_bar_val=1.000000",
        "scale=8",
        "mode=1",
        "fore=0",
        "grid=1",
        "volume=0",
        "scroll=1",
        "shift=1",
        "shift_size=20.000000",
        "fixed_pos=0.000000",
        "ohlc=0",
        "bidline=1",
        "askline=0",
        "lastline=0",
        "days=1",
        "descriptions=0",
        "window_left=0",
        "window_top=0",
        "window_right=0",
        "window_bottom=0",
        "window_type=1",
        "background_color=0",
        "foreground_color=16777215",
        "barup_color=65280",
        "bardown_color=65280",
        "bullcandle_color=0",
        "bearcandle_color=16777215",
        "chartline_color=65280",
        "volumes_color=3329330",
        "grid_color=10061943",
        "bidline_color=10061943",
        "askline_color=255",
        "lastline_color=49152",
        "stops_color=255",
        "windows_total=1",
        "",
        "<expert>",
        "name=MetaEq",
        "path=Experts\\MetaEq.ex5",
        "expertmode=1",
        "<inputs>",
    ]
    lines.extend(f"{key}={value}" for key, value in values.items())
    lines.extend([
        "</inputs>",
        "</expert>",
        "",
        "<window>",
        "height=100.000000",
        "objects=0",
        "",
        "<indicator>",
        "name=Main",
        "path=",
        "apply=1",
        "show_data=1",
        "scale_inherit=0",
        "scale_line=0",
        "scale_line_percent=50",
        "scale_line_value=0.000000",
        "scale_fix_min=0",
        "scale_fix_min_val=0.000000",
        "scale_fix_max=0",
        "scale_fix_max_val=0.000000",
        "expertmode=0",
        "fixed_height=-1",
        "</indicator>",
        "</window>",
        "</chart>",
    ])
    return "\r\n".join(lines) + "\r\n"


def _atomic_replace(path: Path, data: bytes, retries: int = 8):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.15)


def write_text_atomic(path: Path, text: str):
    _atomic_replace(path, text.encode("ascii", errors="replace"))


def write_utf16_atomic(path: Path, text: str):
    _atomic_replace(path, b"\xff\xfe" + text.encode("utf-16-le"))


# --------------------------------------------------------------------------
# Windows process helpers
# --------------------------------------------------------------------------


def memory_mb() -> tuple[int, int]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullAvailPhys // (1024 * 1024)), int(status.ullTotalPhys // (1024 * 1024))
    except (AttributeError, OSError):
        pass
    return 0, 0


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return False


def list_terminal_processes() -> dict[int, str]:
    """Return {pid: executable path} for every terminal64.exe process."""
    result: dict[int, str] = {}
    try:
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return result
    array = (ctypes.c_ulong * 4096)()
    needed = ctypes.c_ulong()
    if not psapi.EnumProcesses(ctypes.byref(array), ctypes.sizeof(array), ctypes.byref(needed)):
        return result
    count = needed.value // ctypes.sizeof(ctypes.c_ulong)
    for pid in array[:count]:
        if not pid:
            continue
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buffer))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                path = buffer.value
                if path.lower().endswith("terminal64.exe"):
                    result[int(pid)] = path
        finally:
            kernel32.CloseHandle(handle)
    return result


def pids_for_dir(directory: Path) -> list[int]:
    try:
        target = str(directory.resolve()).lower().rstrip("\\/")
    except OSError:
        target = str(directory).lower().rstrip("\\/")
    prefix = target + os.sep
    out = []
    for pid, path in list_terminal_processes().items():
        if path.lower().startswith(prefix):
            out.append(pid)
    return out


def launch_terminal(directory: Path, config_path: Path) -> int:
    exe = directory / "terminal64.exe"
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    process = subprocess.Popen(
        [str(exe), "/portable", f"/config:{config_path}"],
        cwd=str(directory),
        close_fds=True,
        creationflags=flags,
    )
    log.info("launched %s (pid %s)", directory, process.pid)
    return int(process.pid)


def kill_pid(pid: int, timeout: float = 20.0):
    if not pid:
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def kill_instance_processes(directory: Path):
    for process_id in pids_for_dir(directory):
        kill_pid(process_id)


def remove_tree(path: Path):
    for attempt in range(3):
        try:
            shutil.rmtree(path)
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2)


# --------------------------------------------------------------------------
# Login confirmation
# --------------------------------------------------------------------------


def current_log_path(directory: Path) -> Path:
    return directory / "logs" / f"{datetime.now().strftime('%Y%m%d')}.log"


def current_log_text(directory: Path) -> str:
    path = current_log_path(directory)
    try:
        return read_text(path) if path.exists() else ""
    except OSError:
        return ""


def classify_login_journal(text: str, login: str) -> tuple[str, str]:
    """Classify the MT5 journal for one login attempt.

    Returns ``(state, detail)`` where state is ``authorized``, ``rejected``,
    ``no_connection``, or ``pending``. Journal lines are evaluated in order:
    a transient "no connection" before a successful "authorized on" is normal
    for Pepperstone and must not fail provisioning, and a reconnect after a
    successful authorization does not undo it. A hard rejection that appears
    after an authorization (for example a retry with a wrong password) does.
    """
    lowered = text.lower()
    if "investor mode" in lowered:
        return "rejected", (
            "MT5 logged in with the investor (read-only) password; "
            "use the master/trading password"
        )
    marker_ok = f"'{login}': authorized on".lower()
    marker_authorization = f"'{login}': authorization on".lower()
    no_connection = f"'{login}': no connection".lower()
    hard_markers = (
        f"'{login}': authorization failed".lower(),
        f"'{login}': invalid account".lower(),
        f"'{login}': invalid password".lower(),
    )
    state = "pending"
    for raw_line in lowered.splitlines():
        line = raw_line.strip()
        if marker_ok in line:
            state = "authorized"
        elif any(marker in line for marker in hard_markers) or (
            marker_authorization in line and " failed" in line
        ):
            state = "rejected"
        elif no_connection in line and state == "pending":
            state = "no_connection"
    if state == "rejected":
        return state, "MT5 rejected the login (check login, server, and trading password)"
    if state == "no_connection":
        return state, "MT5 could not reach the trade server (no connection)"
    if state == "authorized":
        return state, "authorized"
    return state, ""


def wait_for_login(directory: Path, login: str, pid: int, timeout: float, heartbeat,
                   baseline: str = "") -> tuple[bool, str, int]:
    """Watch the MT5 journal for an authorization result. Never logs passwords.

    MT5 can exit and relaunch itself during a first-run LiveUpdate, so process
    liveness is checked by instance directory rather than by the original PID.
    ``baseline`` is the journal text from before launch, so an old "authorized"
    line cannot mask a failed restart.
    """
    deadline = time.time() + timeout
    log_path = current_log_path(directory)
    last_heartbeat = 0.0
    current_pid = int(pid or 0)
    previous_pid = current_pid
    last_process_seen = time.time()
    authorized_at = 0.0
    last_state = "pending"
    while time.time() < deadline:
        log_path = current_log_path(directory)
        instance_pids = pids_for_dir(directory)
        if instance_pids:
            current_pid = int(instance_pids[0])
            if previous_pid and current_pid != previous_pid:
                log.info("MT5 relaunched during startup (pid %s -> %s)", previous_pid, current_pid)
            previous_pid = current_pid
            last_process_seen = time.time()
        elif time.time() - last_process_seen > 30:
            return False, "MT5 exited before confirming the login", current_pid
        if time.time() - last_heartbeat > 20:
            heartbeat(force=True)
            last_heartbeat = time.time()
        try:
            if log_path.exists():
                text = read_text(log_path)
                if baseline and text.startswith(baseline):
                    text = text[len(baseline):]
                last_state, detail = classify_login_journal(text, login)
                if last_state == "rejected":
                    return False, detail, current_pid
                if last_state == "authorized":
                    # The investor-mode line arrives a fraction of a second
                    # after "authorized on". Wait one poll cycle so a
                    # read-only login cannot be reported as healthy.
                    if not authorized_at:
                        authorized_at = time.time()
                    elif time.time() - authorized_at >= 3.0:
                        return True, "authorized", current_pid
                else:
                    authorized_at = 0.0
        except OSError:
            pass
        time.sleep(2)
    if last_state == "no_connection":
        return False, "timed out: MT5 could not reach the trade server (no connection)", current_pid
    return False, "timed out waiting for MT5 to confirm the login", current_pid


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------


def backoff_seconds(attempt: int) -> int:
    return min(300, 15 * (2 ** max(0, attempt - 1)))


def sanitize_error(message: str) -> str:
    text = (message or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:500]


def main():
    setup_logging()
    config = load_config()
    agent = Agent(config)
    try:
        agent.run()
    except KeyboardInterrupt:
        log.info("agent stopped by user")


if __name__ == "__main__":
    main()

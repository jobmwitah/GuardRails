"""Grant discovery and per-account worker supervision.

The backend owns OAuth refresh, so this service only asks the backend for a
short-lived access token, uses it to discover demo accounts, and then lets each
``AccountWorker`` do the actual trading over its own cTrader session.

Nothing here may ever touch a live account: grants are expected to be demo-only
and any ``isLive`` account returned by cTrader is reported to the backend only
so it can be skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from .account import AccountWorker, account_type_name, int_or
from .backend import BackendAuthRequired, BackendClient, BackendError
from .json_client import CTraderJSONClient
from . import protocol

logger = logging.getLogger(__name__)

# Re-scan an active grant every few minutes so a newly opened demo account on
# the same cTID is picked up without asking the user to reconnect.
ACTIVE_DISCOVERY_INTERVAL = 300.0
REAUTH_RETRY_SECONDS = 300.0
WORKER_RETRY_SECONDS = 15.0
TICK_SECONDS = 2.0


@dataclass
class _WorkerSlot:
    worker: AccountWorker
    task: asyncio.Task
    grant_id: int
    started_at: float


class ConnectorService:
    def __init__(self, settings):
        self.settings = settings
        self.backend = BackendClient(
            settings.backend_url, settings.connector_token, timeout=settings.request_timeout
        )
        self.workers: dict[str, _WorkerSlot] = {}
        self._discovered_at: dict[int, float] = {}
        self._reauth_grants: set[int] = set()
        self._retry_after: dict[str, float] = {}
        self._stop = asyncio.Event()

    # ---------- lifecycle ----------

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "cTrader connector starting: backend=%s discovery=%ss report=%ss",
            self.settings.backend_url,
            int(self.settings.discovery_interval),
            int(self.settings.report_interval),
        )
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - the service must keep running
                    logger.exception("connector tick failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        logins = list(self.workers)
        if logins:
            logger.info("stopping %s cTrader account worker(s)", len(logins))
        await asyncio.gather(
            *(self._stop_worker(login) for login in logins), return_exceptions=True
        )
        self.workers.clear()

    # ---------- main tick ----------

    async def _tick(self) -> None:
        now = time.monotonic()
        try:
            grants = await self.backend.get_grants()
        except BackendError as exc:
            logger.warning("could not list cTrader grants: %s", exc)
            return

        grant_by_id = {int_or(grant.get("id")): grant for grant in grants}
        self._reauth_grants = {
            gid for gid, grant in grant_by_id.items()
            if str(grant.get("status") or "") == "reauth"
        }

        due = []
        for gid, grant in grant_by_id.items():
            status = str(grant.get("status") or "")
            if status == "reauth":
                continue
            last = self._discovered_at.get(gid, 0.0)
            interval = self.settings.discovery_interval if status == "pending" else ACTIVE_DISCOVERY_INTERVAL
            if now - last >= interval:
                due.append(grant)
        if due:
            await asyncio.gather(
                *(self._discover(grant) for grant in due), return_exceptions=True
            )

        try:
            accounts = await self.backend.get_accounts()
        except BackendError as exc:
            logger.warning("could not list cTrader accounts: %s", exc)
            return

        await self._sync_workers(accounts, now)

    async def _sync_workers(self, accounts: list[dict], now: float) -> None:
        wanted: dict[str, dict] = {}
        for account in accounts:
            login = str(account.get("login") or "")
            grant_id = int_or(account.get("grant_id"))
            if not login or grant_id <= 0:
                continue
            if str(account.get("environment") or "demo").lower() != "demo":
                continue
            if grant_id in self._reauth_grants:
                continue
            wanted[login] = account

        for login in list(self.workers):
            if login not in wanted:
                logger.info("%s: account removed from backend; stopping worker", login)
                await self._stop_worker(login)

        for login, account in wanted.items():
            slot = self.workers.get(login)
            if slot is not None:
                if not slot.task.done():
                    if slot.grant_id == int_or(account.get("grant_id")):
                        continue
                    # /addctrader reconnected this account under a newer OAuth
                    # grant; replace the session instead of keeping old tokens.
                    logger.info("%s: grant changed; restarting worker", login)
                    await self._stop_worker(login)
                    self._retry_after.pop(login, None)
                else:
                    del self.workers[login]
                    self._log_task_end(login, slot)
            if now < self._retry_after.get(login, 0.0):
                continue
            self._start_worker(account)

    def _start_worker(self, account: dict) -> None:
        login = str(account.get("login") or "")
        grant_id = int_or(account.get("grant_id"))
        worker = AccountWorker(self.settings, self.backend, grant_id, account)
        task = asyncio.create_task(worker.run(), name=f"ctrader-account-{login}")
        self.workers[login] = _WorkerSlot(
            worker=worker, task=task, grant_id=grant_id, started_at=time.monotonic()
        )
        self._retry_after.pop(login, None)
        logger.info("%s: started cTrader account worker (grant %s)", login, grant_id)

    def _log_task_end(self, login: str, slot: _WorkerSlot) -> None:
        task = slot.task
        if task.cancelled():
            logger.info("%s: worker task cancelled", login)
            return
        exc = task.exception()
        if exc is not None:
            if isinstance(exc, BackendAuthRequired):
                logger.error("%s: cTrader connection needs reauthorization: %s", login, exc)
                self._retry_after[login] = time.monotonic() + REAUTH_RETRY_SECONDS
            else:
                logger.error("%s: worker stopped unexpectedly", login, exc_info=exc)
                self._retry_after[login] = time.monotonic() + WORKER_RETRY_SECONDS
            return
        if slot.worker.fatal:
            logger.error("%s: worker entered a fatal state", login)
            self._retry_after[login] = time.monotonic() + REAUTH_RETRY_SECONDS
            return
        logger.warning("%s: worker exited; restarting in %ss", login, int(WORKER_RETRY_SECONDS))
        self._retry_after[login] = time.monotonic() + WORKER_RETRY_SECONDS

    async def _stop_worker(self, login: str) -> None:
        slot = self.workers.pop(login, None)
        if slot is None:
            return
        try:
            await asyncio.wait_for(slot.worker.stop(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - shutdown is best-effort
            pass
        if not slot.task.done():
            slot.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(slot.task, timeout=5.0)

    # ---------- discovery ----------

    async def _discover(self, grant: dict) -> None:
        grant_id = int_or(grant.get("id"))
        if grant_id <= 0:
            return
        # Record the attempt before the network call so a slow/failing grant
        # cannot be retried in a tight loop by every tick.
        self._discovered_at[grant_id] = time.monotonic()
        try:
            session = await self.backend.get_session(grant_id)
        except BackendAuthRequired as exc:
            logger.error("grant %s needs reauthorization: %s", grant_id, exc)
            self._reauth_grants.add(grant_id)
            return
        except BackendError as exc:
            logger.warning("grant %s: could not get a session: %s", grant_id, exc)
            return

        environment = str(session.get("environment") or "demo").lower()
        if environment != "demo":
            logger.error("grant %s is not a demo grant; refusing discovery", grant_id)
            return
        access_token = str(session.get("access_token") or "")
        if not access_token:
            logger.warning("grant %s: backend returned an empty access token", grant_id)
            return

        accounts = await self._read_accounts(access_token)
        if accounts is None:
            return

        registered_demo = 0
        for item in accounts:
            if item["is_live"]:
                continue
            registered_demo += 1
        try:
            result = await self.backend.post_grant_accounts(grant_id, accounts)
        except BackendError as exc:
            logger.warning("grant %s: could not register accounts: %s", grant_id, exc)
            return

        added = result.get("added") or []
        skipped = result.get("skipped") or []
        logger.info(
            "grant %s: discovered %s demo account(s), %s already registered, %s skipped",
            grant_id,
            registered_demo,
            len(result.get("registered") or []) - len(added),
            len(skipped),
        )
        if added:
            logger.info("grant %s: new account(s): %s", grant_id, ", ".join(added))
        if skipped:
            logger.warning("grant %s: skipped accounts: %s", grant_id, skipped)

    async def _read_accounts(self, access_token: str) -> list[dict] | None:
        """Return backend-shaped account rows for every cTID account.

        Live accounts are returned with ``is_live`` set so the backend can
        explicitly skip them; they are never account-authenticated here.
        """
        client = CTraderJSONClient(
            self.settings.host_for("demo"),
            self.settings.port,
            connect_timeout=self.settings.connect_timeout,
            request_timeout=self.settings.request_timeout,
        )
        try:
            await client.connect()
            await client.request(
                protocol.APP_AUTH_REQ,
                {
                    "clientId": self.settings.client_id,
                    "clientSecret": self.settings.client_secret,
                },
                expect=protocol.APP_AUTH_RES,
            )
            response = await client.request(
                protocol.GET_ACCOUNTS_REQ,
                {"accessToken": access_token},
                expect=protocol.GET_ACCOUNTS_RES,
            )
            raw_accounts = (response.get("payload") or {}).get("ctidTraderAccount") or []
            result: list[dict] = []
            for raw in raw_accounts:
                ctid = int_or(raw.get("ctidTraderAccountId"))
                if ctid <= 0:
                    continue
                is_live = bool(raw.get("isLive"))
                trader_login = str(raw.get("traderLogin") or "").strip()
                item = {
                    "ctid_trader_account_id": str(ctid),
                    "is_live": is_live,
                    "trader_login": trader_login,
                }
                if not is_live:
                    item.update(await self._demo_account_details(client, access_token, ctid))
                result.append(item)
            return result
        except (ConnectionError, OSError, protocol.ProtocolError) as exc:
            logger.warning("grant discovery failed while talking to cTrader: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - never crash the supervisor
            logger.exception("grant discovery failed")
            return None
        finally:
            await client.close()

    async def _demo_account_details(self, client: CTraderJSONClient, access_token: str,
                                    ctid: int) -> dict:
        details = {
            "ctid_trader_account_id": str(ctid),
            "is_live": False,
            "trader_login": "",
            "broker_name": "",
            "account_type": None,
            "display_name": "",
        }
        try:
            await client.request(
                protocol.ACCOUNT_AUTH_REQ,
                {"ctidTraderAccountId": ctid, "accessToken": access_token},
                expect=protocol.ACCOUNT_AUTH_RES,
            )
            response = await client.request(
                protocol.TRADER_REQ,
                {"ctidTraderAccountId": ctid},
                expect=protocol.TRADER_RES,
            )
        except (protocol.ProtocolError, ConnectionError, OSError) as exc:
            logger.warning(
                "could not read cTrader account %s metadata (registration continues): %s",
                ctid, exc,
            )
            return details

        trader = (response.get("payload") or {}).get("trader") or {}
        broker = str(trader.get("brokerName") or "").strip()
        trader_login = str(trader.get("traderLogin") or "").strip()
        type_name = account_type_name(trader.get("accountType"))
        details.update({
            "trader_login": trader_login,
            "broker_name": broker,
            "account_type": {"HEDGED": 0, "NETTED": 1, "SPREAD_BETTING": 2}.get(type_name),
            "display_name": (
                f"{broker} · {trader_login}" if broker and trader_login
                else broker or (f"cTrader {ctid}" if not trader_login else trader_login)
            ),
        })
        return details

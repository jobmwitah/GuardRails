"""End-to-end tests for MetaEq attach/sync jobs through the agent API."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("OWNER_DISCORD_ID", "1")
os.environ.setdefault("BRIDGE_TOKEN", "bridge-test")
os.environ.setdefault("AGENT_TOKEN", "agent-test")
os.environ.setdefault("CREDENTIAL_KEY", Fernet.generate_key().decode())


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content, ephemeral=False):
        self.messages.append(content)


class FakeInteraction:
    def __init__(self, owner_id):
        self.user = SimpleNamespace(id=owner_id)
        self.response = FakeResponse()


class MetaEqJobFlowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = str(Path(self.tempdir.name) / "test.db")

        import storage
        self.db = storage
        self.db.DB_PATH = os.environ["DB_PATH"]
        self.db.init_db()

        import main
        self.main = main
        main.accounts.clear()
        main.default_account = None
        self.login = "61603648"
        acc = main.AccountState(
            login=self.login,
            phase="challenge",
            risk_pct=1.0,
            suffix="0",
            server="Pepperstone-Demo",
            instance_status="running",
            instance_dir=r"C:\GuardRails\instances\61603648",
        )
        main.accounts[self.login] = acc
        main.default_account = self.login
        main.persist_account(acc)

        self.client = TestClient(main.app)
        self.headers = {"x-agent-token": "agent-test", "x-agent-id": "windows-1"}

    def tearDown(self):
        import main
        main.accounts.clear()
        self.tempdir.cleanup()
        if self.old_path is not None:
            os.environ["DB_PATH"] = self.old_path

    def interaction(self):
        return FakeInteraction(self.main.OWNER_ID)

    def test_attach_job_then_toggle_coalesces_and_detach_clears(self):
        interaction = self.interaction()
        asyncio.run(self.main.metaeq_attach.callback(interaction, self.login, None))
        self.assertIn("attach queued", interaction.response.messages[0].lower())

        response = self.client.get("/agent/jobs", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        job = response.json()["job"]
        self.assertEqual(job["action"], "metaeq_apply")
        self.assertEqual(job["payload"]["chart_symbol"], "NAS100")
        self.assertEqual(job["payload"]["suffix"], "0")
        self.assertIn("S1=1", job["payload"]["control"])
        job_id = job["id"]

        result = self.client.post(
            f"/agent/jobs/{job_id}/result",
            headers=self.headers,
            json={"status": "done", "result": {"attached": True}},
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.db.get_metaeq_account(self.login)["attached"], 1)

        self.db.upsert_metaeq_account(self.login, True, "0", "NAS100")
        toggle = self.interaction()
        asyncio.run(self.main.metaeq_toggle.callback(toggle, "S4", False))
        asyncio.run(self.main.metaeq_toggle.callback(toggle, "S7", False))
        pending = self.db.get_pending_job_for_login(self.login, ("metaeq_apply",))
        self.assertIsNotNone(pending)

        response = self.client.get("/agent/jobs", headers=self.headers)
        job = response.json()["job"]
        self.assertEqual(job["id"], pending["id"])
        self.assertEqual(job["action"], "metaeq_apply")
        self.assertIn("S4=0", job["payload"]["control"])
        self.assertIn("S7=0", job["payload"]["control"])
        self.client.post(
            f"/agent/jobs/{job['id']}/result",
            headers=self.headers,
            json={"status": "done", "result": {"attached": True}},
        )

        detach = self.interaction()
        asyncio.run(self.main.metaeq_detach.callback(detach, self.login))
        self.assertIn("detach queued", detach.response.messages[0].lower())
        response = self.client.get("/agent/jobs", headers=self.headers)
        job = response.json()["job"]
        self.assertEqual(job["action"], "metaeq_detach")
        self.assertEqual(self.db.get_metaeq_account(self.login)["attached"], 0)
        self.client.post(
            f"/agent/jobs/{job['id']}/result",
            headers=self.headers,
            json={"status": "done", "result": {"attached": False}},
        )
        self.assertEqual(self.db.get_metaeq_account(self.login)["attached"], 0)

    def test_regime_queues_control_for_every_usdjpy_strategy(self):
        self.db.upsert_metaeq_account(self.login, True, "0", "NAS100")
        interaction = self.interaction()
        asyncio.run(self.main.metaeq_regime.callback(interaction, "USDJPY", "ranging"))

        response = self.client.get("/agent/jobs", headers=self.headers)
        job = response.json()["job"]
        control = job["payload"]["control"]
        self.assertIn("S2A=0", control)
        self.assertIn("S3B=0", control)
        self.assertIn("S5=0", control)
        self.assertIn("S7=0", control)
        self.assertIn("S1=1", control)

        self.client.post(
            f"/agent/jobs/{job['id']}/result",
            headers=self.headers,
            json={"status": "done", "result": {"attached": True}},
        )
        self.client.get("/agent/jobs", headers=self.headers)  # no more pending jobs
        self.assertIsNone(self.db.get_pending_job_for_login(self.login))

    def test_agent_result_is_idempotent(self):
        job_id = self.db.create_provision_job(
            self.login, "metaeq_apply", self.main.provisioning.encrypt_json({"login": self.login})
        )
        first = self.client.post(
            f"/agent/jobs/{job_id}/result",
            headers=self.headers,
            json={"status": "done", "result": "ok"},
        )
        self.assertEqual(first.status_code, 200)
        second = self.client.post(
            f"/agent/jobs/{job_id}/result",
            headers=self.headers,
            json={"status": "done", "result": "ok"},
        )
        self.assertEqual(second.status_code, 409)


if __name__ == "__main__":
    unittest.main()

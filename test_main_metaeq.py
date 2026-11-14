"""Tests for the Discord-facing MetaEq Manager helpers."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("OWNER_DISCORD_ID", "1")
os.environ.setdefault("BRIDGE_TOKEN", "test-bridge")


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content, ephemeral=False):
        self.messages.append(content)


class FakeInteraction:
    def __init__(self, owner_id=1):
        self.user = SimpleNamespace(id=owner_id)
        self.response = FakeResponse()


class MetaEqCommandTests(unittest.TestCase):
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
        self.acc = main.AccountState(
            login=self.login,
            phase="challenge",
            suffix=".a",
            instance_dir=r"C:\GuardRails\instances\61603648",
            last_report={"metaeq": "1", "metaeq_symbol": "NAS100.a"},
        )
        main.accounts[self.login] = self.acc
        main.default_account = self.login

    def tearDown(self):
        import main
        main.accounts.clear()
        self.tempdir.cleanup()
        if self.old_path is not None:
            os.environ["DB_PATH"] = self.old_path

    def test_command_group_is_registered(self):
        names = {command.name for command in self.main.metaeq_group.commands}
        self.assertEqual(
            names,
            {"attach", "detach", "status", "toggle", "pair", "regime", "sync"},
        )

    def test_payload_uses_suffix_and_saved_toggles(self):
        self.db.set_metaeq_strategy(self.login, "S4", False)
        payload = self.main._metaeq_payload(self.login)
        self.assertEqual(payload["suffix"], ".a")
        self.assertEqual(payload["chart_symbol"], "NAS100.a")
        self.assertEqual(payload["symbols"]["XAUUSD"], "XAUUSD.a")
        self.assertFalse(self.main.metaeq.parse_control_text(payload["control"])["S4"])
        self.assertTrue(self.main.metaeq.parse_control_text(payload["control"])["S1"])

    def test_status_lines_show_pair_membership_and_regime(self):
        self.db.upsert_metaeq_account(self.login, True, ".a", "NAS100.a")
        self.db.set_metaeq_pair_ranging(self.login, "USDJPY", True)
        self.db.set_metaeq_strategy(self.login, "S7", True)
        lines = "\n".join(self.main.metaeq_status_lines(self.login))
        self.assertIn("MetaEq", lines)
        self.assertIn("`61603648`", lines)
        self.assertIn("EA: live on `NAS100.a`", lines)
        self.assertRegex(lines, r"\*\*NAS100\*\* \(`NAS100\.a`\).*trending")
        self.assertRegex(lines, r"\*\*USDJPY\*\* \(`USDJPY\.a`\).*ranging")
        self.assertIn("ranging-sensitive: S2A ON, S3B ON, S5 ON", lines)
        self.assertIn("not affected: S7 ON", lines)

    def test_regime_ranging_only_touches_affected_strategies(self):
        interaction = FakeInteraction(self.main.OWNER_ID)
        asyncio.run(self.main.metaeq_regime.callback(interaction, "NAS100", "ranging"))
        states = self.db.load_metaeq_strategies(self.login)
        self.assertFalse(states["S1"])
        self.assertFalse(states["S3"])
        self.assertFalse(states["S4"])
        self.assertFalse(states["S8"])
        self.assertTrue(self.main._metaeq_states(self.login)["S9"])
        self.assertTrue(self.db.load_metaeq_pairs(self.login)["NAS100"])
        self.assertIn("now **ranging**", interaction.response.messages[0])

    def test_pair_and_toggle_save_without_an_attach_job(self):
        self.db.upsert_metaeq_account(self.login, False, ".a", "NAS100.a")
        interaction = FakeInteraction(self.main.OWNER_ID)
        asyncio.run(self.main.metaeq_pair.callback(interaction, "XAUUSD", False))
        states = self.db.load_metaeq_strategies(self.login)
        self.assertFalse(states["S2B"])
        self.assertFalse(states["S6"])
        self.assertNotIn("XAUUSD", self.db.load_metaeq_pairs(self.login))

        asyncio.run(self.main.metaeq_toggle.callback(interaction, "S6", True))
        self.assertTrue(self.db.load_metaeq_strategies(self.login)["S6"])
        self.assertIn("next `/metaeq attach`", interaction.response.messages[-1])


if __name__ == "__main__":
    unittest.main()

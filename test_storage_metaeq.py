"""Storage tests for MetaEq Manager state and job coalescing."""

import tempfile
import unittest
from pathlib import Path

import storage


class StorageTestBase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_path = storage.DB_PATH
        storage.DB_PATH = str(Path(self.tempdir.name) / "test.db")
        storage.init_db()

    def tearDown(self):
        storage.DB_PATH = self.old_path
        self.tempdir.cleanup()


class MetaEqStateTests(StorageTestBase):
    def test_strategy_round_trip(self):
        self.assertEqual(storage.load_metaeq_strategies("61603648"), {})
        storage.save_metaeq_strategies(
            "61603648", {"S1": True, "S4": False, "S9": True}
        )
        self.assertEqual(
            storage.load_metaeq_strategies("61603648"),
            {"S1": True, "S4": False, "S9": True},
        )
        storage.set_metaeq_strategy("61603648", "S4", True)
        self.assertTrue(storage.load_metaeq_strategies("61603648")["S4"])

    def test_pair_regime_round_trip(self):
        self.assertEqual(storage.load_metaeq_pairs("61603648"), {})
        storage.set_metaeq_pair_ranging("61603648", "NAS100", True)
        storage.set_metaeq_pair_ranging("61603648", "XAUUSD", False)
        self.assertEqual(
            storage.load_metaeq_pairs("61603648"),
            {"NAS100": True, "XAUUSD": False},
        )

    def test_account_error_is_preserved_when_not_supplied(self):
        storage.upsert_metaeq_account("61603648", True, "0", "NAS100")
        row = storage.get_metaeq_account("61603648")
        self.assertEqual(row["attached"], 1)
        self.assertEqual(row["last_error"], "")

        storage.mark_metaeq_synced("61603648", "chart failed")
        storage.upsert_metaeq_account("61603648", True, "0", "NAS100")
        self.assertEqual(storage.get_metaeq_account("61603648")["last_error"], "chart failed")

        storage.upsert_metaeq_account("61603648", True, "0", "NAS100", last_error="boom")
        self.assertEqual(storage.get_metaeq_account("61603648")["last_error"], "boom")

    def test_delete_account_removes_metaeq_rows(self):
        storage.upsert_metaeq_account("61603648", True, "0", "NAS100")
        storage.save_metaeq_strategies("61603648", {"S1": False})
        storage.set_metaeq_pair_ranging("61603648", "NAS100", True)
        storage.delete_account("61603648")
        self.assertIsNone(storage.get_metaeq_account("61603648"))
        self.assertEqual(storage.load_metaeq_strategies("61603648"), {})
        self.assertEqual(storage.load_metaeq_pairs("61603648"), {})


class JobCoalescingTests(StorageTestBase):
    def test_pending_metaeq_job_is_reused(self):
        job_id = storage.create_provision_job("61603648", "metaeq_apply", "first")
        pending = storage.get_pending_job_for_login("61603648", ("metaeq_apply",))
        self.assertEqual(pending["id"], job_id)

        self.assertTrue(storage.update_pending_job(job_id, "metaeq_apply", "second"))
        same = storage.get_pending_job_for_login("61603648", ("metaeq_apply",))
        self.assertEqual(same["id"], job_id)
        self.assertEqual(same["payload_enc"], "second")

    def test_claimed_job_is_not_updated(self):
        job_id = storage.create_provision_job("61603648", "metaeq_apply", "first")
        claimed = storage.claim_next_job("windows-1")
        self.assertEqual(claimed["id"], job_id)
        self.assertFalse(storage.update_pending_job(job_id, "metaeq_apply", "second"))

        new_id = storage.create_provision_job("61603648", "metaeq_apply", "third")
        self.assertNotEqual(new_id, job_id)


if __name__ == "__main__":
    unittest.main()

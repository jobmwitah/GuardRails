"""Copy-trading helper and persistence tests (no Discord/FastAPI imports)."""

import os
import tempfile
import unittest

import copy_trading
import storage


class SymbolResolutionTests(unittest.TestCase):
    def test_exact_match_wins(self):
        self.assertEqual(
            copy_trading.slave_symbol_for("EURUSD", "0", ["EURUSD.m", "EURUSD"], "m"),
            "EURUSD",
        )

    def test_master_suffix_is_removed(self):
        self.assertEqual(
            copy_trading.slave_symbol_for("EURUSD.m", "m", ["EURUSD.raw"], "raw"),
            "EURUSD.raw",
        )

    def test_instrument_root_matches_across_brokers(self):
        self.assertEqual(
            copy_trading.slave_symbol_for("US100.cash", "cash", ["US100"], "0"),
            "US100",
        )

    def test_decorated_slave_symbol(self):
        self.assertEqual(
            copy_trading.slave_symbol_for("US100", "0", ["US100.cash"], "0"),
            "US100.cash",
        )

    def test_unknown_symbol_is_not_invented(self):
        self.assertIsNone(
            copy_trading.slave_symbol_for("EURUSD", "0", ["GBPUSD"], "0")
        )

    def test_case_insensitive(self):
        self.assertEqual(
            copy_trading.slave_symbol_for("eurusd", "0", ["EURUSD.pro"], "pro"),
            "EURUSD.pro",
        )


class CommandTests(unittest.TestCase):
    def test_price_matches_at_reported_precision(self):
        self.assertTrue(copy_trading.price_matches("1.080004", "1.08000"))
        self.assertTrue(copy_trading.price_matches("1.08", "1.08000"))
        self.assertTrue(copy_trading.price_matches("20000.4", "20000"))
        self.assertTrue(copy_trading.price_matches("61141.049999", "61141.05"))
        self.assertFalse(copy_trading.price_matches("1.08", "1.08001"))
        self.assertFalse(copy_trading.price_matches("", "1.08"))
        self.assertFalse(copy_trading.price_matches("0", "1.08"))

    def test_market_command_uses_slave_risk_and_rrr(self):
        command = copy_trading.order_command(
            "market", "BUY", "EURUSD", 0.0, 1.08, 0.25, 3.25
        )
        self.assertEqual(command, "OPEN_MARKET BUY EURUSD 1.08 0.25 3.25")

    def test_pending_buy_recomputes_tp_at_slave_rr(self):
        command = copy_trading.order_command(
            "pending", "BUY", "EURUSD", 1.09, 1.08, 0.5, 3.25
        )
        self.assertEqual(command, "OPEN BUY EURUSD 1.09 1.08 1.1225 0.5")

    def test_pending_sell_recomputes_tp_at_slave_rr(self):
        command = copy_trading.order_command(
            "pending", "SELL", "EURUSD", 1.09, 1.10, 0.5, 3.25
        )
        self.assertEqual(command, "OPEN SELL EURUSD 1.09 1.1 1.0575 0.5")

    def test_pending_rejects_stop_on_wrong_side(self):
        with self.assertRaises(ValueError):
            copy_trading.order_command(
                "pending", "BUY", "EURUSD", 1.08, 1.09, 0.5, 3.25
            )

    def test_missing_sl_is_rejected(self):
        with self.assertRaises(ValueError):
            copy_trading.order_command(
                "market", "BUY", "EURUSD", 0.0, 0.0, 0.5, 3.25
            )

    def test_pending_side_labels(self):
        self.assertEqual(copy_trading.pending_side("BUY_LIMIT"), "BUY")
        self.assertEqual(copy_trading.pending_side("SELL_STOP"), "SELL")

    def test_price_formatting(self):
        self.assertEqual(copy_trading.format_price(1.1234500), "1.12345")
        self.assertEqual(copy_trading.format_price(1.0), "1")


class CopyStorageTests(unittest.TestCase):
    def setUp(self):
        self._old_path = storage.DB_PATH
        handle, self._path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        storage.DB_PATH = self._path
        storage.init_db()

    def tearDown(self):
        storage.DB_PATH = self._old_path
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def test_default_is_disabled_and_empty(self):
        cfg = storage.get_copy_config()
        self.assertEqual(cfg["master_login"], "")
        self.assertEqual(cfg["enabled"], 0)
        self.assertEqual(cfg["baselined"], 0)

    def test_config_round_trip(self):
        storage.set_copy_config("26899785", True, baselined=False)
        cfg = storage.get_copy_config()
        self.assertEqual(cfg["master_login"], "26899785")
        self.assertEqual(cfg["enabled"], 1)
        self.assertEqual(cfg["baselined"], 0)
        storage.set_copy_baselined(True)
        self.assertEqual(storage.get_copy_config()["baselined"], 1)

    def test_slaves_are_opt_in_and_deduplicated(self):
        storage.add_copy_slave("111")
        storage.add_copy_slave("222")
        storage.add_copy_slave("111")
        self.assertEqual(
            [row["login"] for row in storage.list_copy_slaves(enabled_only=True)],
            ["111", "222"],
        )
        storage.remove_copy_slave("111")
        self.assertEqual(
            [row["login"] for row in storage.list_copy_slaves()], ["222"]
        )
        storage.clear_copy_slaves()
        self.assertEqual(storage.list_copy_slaves(), [])

    def test_link_is_idempotent_and_lookupable(self):
        first = storage.create_copy_link(
            "master", "100", "pending", "EURUSD", "BUY", "1.09", "1.08",
            master_order_ticket="100",
        )
        second = storage.create_copy_link(
            "master", "100", "pending", "EURUSD", "BUY", "1.09", "1.08"
        )
        self.assertEqual(first, second)
        link = storage.get_copy_link_by_master("master", "100", "pending")
        self.assertEqual(link["symbol"], "EURUSD")
        self.assertEqual(link["master_order_ticket"], "100")
        self.assertEqual(storage.get_copy_link_by_order("master", "100")["id"], first)

    def test_link_update_whitelist(self):
        link_id = storage.create_copy_link("master", "200", "position", "EURUSD", "SELL")
        storage.update_copy_link(link_id, status="open", reason="triggered", bogus="x")
        link = storage.get_copy_link(link_id)
        self.assertEqual(link["status"], "open")
        self.assertEqual(link["reason"], "triggered")
        self.assertNotIn("bogus", link)

    def test_legs_join_link_and_match_ticket(self):
        link_id = storage.create_copy_link(
            "master", "300", "position", "EURUSD", "BUY", "0", "1.08"
        )
        leg_id = storage.add_copy_leg(link_id, "slave-1")
        self.assertEqual(leg_id, storage.add_copy_leg(link_id, "slave-1"))
        storage.update_copy_leg(leg_id, slave_ticket="9001", status="open")
        leg = storage.find_copy_leg_by_ticket("slave-1", "9001")
        self.assertEqual(leg["master_login"], "master")
        self.assertEqual(leg["link_symbol"], "EURUSD")
        self.assertEqual(leg["status"], "open")
        joined = storage.list_copy_legs_for_slave("slave-1")
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0]["master_ticket"], "300")

    def test_seen_tickets_persist_and_clear(self):
        storage.add_copy_seen_many("master", [("1", "position"), ("2", "pending")])
        self.assertEqual(
            storage.load_copy_seen("master"),
            {("1", "position"), ("2", "pending")},
        )
        storage.clear_copy_seen("master")
        self.assertEqual(storage.load_copy_seen("master"), set())

    def test_forget_slave_marks_inflight_legs_skipped(self):
        link_id = storage.create_copy_link("master", "400", "position", "EURUSD", "BUY")
        queued = storage.add_copy_leg(link_id, "slave-1")
        storage.forget_copy_slave_legs("slave-1")
        self.assertEqual(
            storage.get_copy_legs(link_id)[0]["status"], "skipped"
        )

    def test_close_links_marks_open_and_pending(self):
        storage.create_copy_link("master", "500", "position", "EURUSD", "BUY")
        storage.create_copy_link("master", "501", "pending", "EURUSD", "SELL")
        self.assertEqual(storage.close_copy_links("master", "orphaned"), 2)
        self.assertEqual(
            storage.list_copy_links("master", statuses=("open", "pending")), []
        )


if __name__ == "__main__":
    unittest.main()

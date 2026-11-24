import os
import tempfile
import unittest

import storage


class BrokerTradeStorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_path = storage.DB_PATH
        storage.DB_PATH = os.path.join(self.tempdir.name, "test.db")
        storage.init_db()

    def tearDown(self):
        storage.DB_PATH = self.previous_path
        self.tempdir.cleanup()

    def test_upsert_is_idempotent_by_deal_id(self):
        rows = [
            {
                "deal_id": "101",
                "position_id": "55",
                "time": 1000.0,
                "symbol": "BTCUSD",
                "side": "SELL",
                "volume": "0.12",
                "profit": -8.76,
            },
            {
                "deal_id": "102",
                "position_id": "56",
                "time": 2000.0,
                "symbol": "BTCUSD",
                "side": "BUY",
                "volume": "0.05",
                "profit": 0.65,
            },
        ]
        self.assertEqual(storage.upsert_broker_trades("ctrader:1", rows), 2)
        self.assertEqual(storage.upsert_broker_trades("ctrader:1", rows), 0)

        loaded = storage.load_broker_trades("ctrader:1")
        self.assertEqual([row["deal_id"] for row in loaded], ["102", "101"])
        self.assertEqual(loaded[0]["source"], "broker")

    def test_rows_are_scoped_to_the_account(self):
        storage.upsert_broker_trades("ctrader:1", [{
            "deal_id": "201", "position_id": "60", "time": 1000.0,
            "symbol": "EURUSD", "side": "BUY", "volume": "0.10", "profit": 1.0,
        }])
        self.assertEqual(storage.load_broker_trades("ctrader:2"), [])
        self.assertEqual(len(storage.load_broker_trades("ctrader:1")), 1)

    def test_source_is_preserved_for_recovered_bot_deals(self):
        storage.upsert_broker_trades("ctrader:1", [{
            "deal_id": "401", "position_id": "80", "time": 3000.0,
            "symbol": "BTCUSD", "side": "SELL", "volume": "0.12",
            "profit": -8.76, "source": "broker_bot",
        }])
        loaded = storage.load_broker_trades("ctrader:1")
        self.assertEqual(loaded[0]["source"], "broker_bot")

    def test_delete_account_removes_broker_history(self):
        storage.upsert_broker_trades("ctrader:1", [{
            "deal_id": "301", "position_id": "70", "time": 1000.0,
            "symbol": "EURUSD", "side": "SELL", "volume": "0.10", "profit": -1.0,
        }])
        storage.delete_account("ctrader:1")
        self.assertEqual(storage.load_broker_trades("ctrader:1"), [])


if __name__ == "__main__":
    unittest.main()

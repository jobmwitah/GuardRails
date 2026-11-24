import unittest

import mt5_history


class ParseMt5DealsTests(unittest.TestCase):
    def test_parses_manual_and_bot_rows(self):
        raw = (
            "101,55,1700000000,EURUSD,BUY,0.10,12.34,broker|"
            "102,56,1700000100,XAUUSD,SELL,0.02,-5.00,broker_bot"
        )
        rows = mt5_history.parse_deals(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {
            "deal_id": "101",
            "position_id": "55",
            "time": 1700000000.0,
            "symbol": "EURUSD",
            "side": "BUY",
            "volume": "0.10",
            "profit": 12.34,
            "source": "broker",
        })
        self.assertEqual(rows[1]["source"], "broker_bot")

    def test_skips_malformed_rows_and_unknown_sources(self):
        raw = (
            "bad|"
            "101,55,not-a-time,EURUSD,BUY,0.10,1.00,broker|"
            "102,56,1700000000,EURUSD,SIDEWAYS,0.10,1.00,broker|"
            "103,57,1700000000,EURUSD,BUY,0.10,1.00,external"
        )
        rows = mt5_history.parse_deals(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deal_id"], "103")
        self.assertEqual(rows[0]["source"], "broker")

    def test_deduplicates_by_deal_id(self):
        raw = (
            "101,55,1700000000,EURUSD,BUY,0.10,1.00,broker|"
            "101,55,1700000000,EURUSD,BUY,0.10,1.00,broker"
        )
        self.assertEqual(len(mt5_history.parse_deals(raw)), 1)

    def test_caps_the_snapshot(self):
        raw = "|".join(
            f"{i},{i},1700000000,EURUSD,BUY,0.10,1.00,broker"
            for i in range(1, mt5_history.MAX_HISTORY_ROWS + 50)
        )
        self.assertEqual(
            len(mt5_history.parse_deals(raw)),
            mt5_history.MAX_HISTORY_ROWS,
        )

    def test_empty_snapshot(self):
        self.assertEqual(mt5_history.parse_deals(""), [])


if __name__ == "__main__":
    unittest.main()

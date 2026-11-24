"""Mobile order confirmation and broker-symbol cleanup tests.

These exercise the backend helpers without starting FastAPI, Discord or any
broker connection.
"""

import unittest
from unittest.mock import patch

import main


class QuoteValidationTests(unittest.TestCase):
    def test_zero_quote_rejects_unresolved_broker_symbol(self):
        acc = main.AccountState(login="ctrader:1")
        # The cTrader connector writes a zero ask/bid pair when its broker
        # symbol list has no match for an enabled root (for example NAS100).
        acc.last_report = {"nas100_ask": "0", "nas100_bid": "0"}
        self.assertTrue(main.is_market_closed_for(acc, "NAS100"))

    def test_missing_quote_fields_fail_open(self):
        acc = main.AccountState(login="1")
        acc.last_report = {"balance": "1000"}
        self.assertFalse(main.is_market_closed_for(acc, "NAS100"))


class OrderConfirmationTests(unittest.TestCase):
    def test_new_market_position_confirms_the_order(self):
        before = {"10"}
        report = {"positions": "EURUSD,10,BUY,0.10,0.00|NAS100,11,BUY,0.20,5.00"}
        self.assertEqual(
            main._confirmed_order_ticket(report, "market", "BUY", "NAS100", before),
            "11",
        )

    def test_pre_existing_ticket_is_not_confirmation(self):
        report = {"positions": "NAS100,10,BUY,0.20,5.00"}
        self.assertEqual(
            main._confirmed_order_ticket(report, "market", "BUY", "NAS100", {"10"}),
            "",
        )

    def test_limit_pending_confirms_the_order(self):
        report = {"pending": "NAS100,21,BUY_LIMIT,26000,25900,26300"}
        self.assertEqual(
            main._confirmed_order_ticket(report, "limit", "BUY", "NAS100", set()),
            "21",
        )

    def test_rejection_after_submit_is_returned(self):
        acc = main.AccountState(login="ctrader:1")
        acc.recent_events = [
            (100.0, "ORDER_REJECTED EURUSD invalid_sl"),
            (175.0, "ORDER_REJECTED NAS100 symbol_unavailable"),
        ]
        self.assertEqual(
            main._order_rejection_reason(acc, "NAS100", 150.0),
            "symbol_unavailable",
        )
        self.assertEqual(main._order_rejection_reason(acc, "NAS100", 200.0), "")

    def test_symbol_unavailable_event_is_a_rejection(self):
        acc = main.AccountState(login="ctrader:1")
        acc.recent_events = [(100.0, "SYMBOL_UNAVAILABLE NAS100")]
        self.assertEqual(
            main._order_rejection_reason(acc, "nas100", 50.0),
            "symbol_unavailable",
        )


class SymbolCleanupTests(unittest.TestCase):
    def test_drop_account_symbol_removes_and_notifies(self):
        acc = main.AccountState(login="ctrader:1", symbols=["EURUSD", "NAS100"])
        with patch.object(main.db, "remove_symbol") as remove, \
                patch.object(main.db, "enqueue_notification") as notify:
            stored = main.drop_account_symbol(
                acc, "nas100", "the broker does not offer that symbol"
            )

        self.assertEqual(stored, "NAS100")
        self.assertEqual(acc.symbols, ["EURUSD"])
        remove.assert_called_once_with("ctrader:1", "NAS100")
        notify.assert_called_once()
        self.assertIn("NAS100", notify.call_args.args[0])

    def test_unknown_symbol_is_left_alone(self):
        acc = main.AccountState(login="ctrader:1", symbols=["EURUSD"])
        with patch.object(main.db, "remove_symbol") as remove:
            self.assertIsNone(main.drop_account_symbol(acc, "NAS100", "unavailable"))
        remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()

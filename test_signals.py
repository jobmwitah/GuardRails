"""Dev-only checks for inbound webhook signal parsing and dedupe."""

import json
import os
import tempfile
import unittest

import signals
import storage as db

KNOWN = ["EURUSD", "GBPUSD", "XAUUSD", "US30"]


class SignalParsingTests(unittest.TestCase):
    def test_plain_text_message(self):
        parsed = signals.parse_signal("BUY EURUSD SL=1.0800", known_symbols=KNOWN)
        self.assertEqual(parsed.side, "BUY")
        self.assertEqual(parsed.symbol, "EURUSD")
        self.assertAlmostEqual(parsed.sl, 1.08)
        self.assertEqual(parsed.conflicts, [])
        self.assertTrue(parsed.ok)

    def test_openmarket_style_payload(self):
        parsed = signals.parse_signal(
            {
                "symbol": "FX_OTC:EURUSD",
                "condition": "crossing_up",
                "price": 1.0850,
                "message": "EUR/USD crossed above 1.0850 — BUY SL=1.0820",
                "source": "openmarket",
            },
            known_symbols=KNOWN,
        )
        self.assertEqual(parsed.symbol, "EURUSD")
        self.assertEqual(parsed.side, "BUY")
        self.assertAlmostEqual(parsed.sl, 1.082)
        self.assertEqual(parsed.platform, "openmarket")
        self.assertEqual(parsed.message, "EUR/USD crossed above 1.0850 — BUY SL=1.0820")

    def test_structured_tradingview_style_payload(self):
        parsed = signals.parse_signal(
            {
                "ticker": "GBP/USD",
                "action": "sell",
                "stop_loss": "1.2700",
                "message": "SELL GBP/USD SL=1.2700",
            },
            known_symbols=KNOWN,
        )
        self.assertEqual(parsed.symbol, "GBPUSD")
        self.assertEqual(parsed.side, "SELL")
        self.assertAlmostEqual(parsed.sl, 1.27)

    def test_symbol_formats(self):
        cases = {
            "FX_OTC:EURUSD": "EURUSD",
            "EUR/USD": "EURUSD",
            "EUR_USD": "EURUSD",
            "EURUSD.m": "EURUSD.M",
            "US30": "US30",
            "XAU/USD": "XAUUSD",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                parsed = signals.parse_signal({"symbol": raw, "side": "buy", "sl": 1.0})
                self.assertEqual(parsed.symbol, expected)

    def test_venue_name_is_not_treated_as_symbol(self):
        parsed = signals.parse_signal(
            {"market": "FX_OTC", "side": "buy", "sl": 1.08, "message": "BUY SL=1.08"}
        )
        self.assertIsNone(parsed.symbol)
        self.assertIn("symbol", parsed.missing)

    def test_nested_valid_field_beats_invalid_top_level(self):
        parsed = signals.parse_signal({
            "symbol": "FX_OTC",
            "data": {"symbol": "EURUSD", "account": "1345657215"},
            "side": "buy",
            "sl": 1.08,
        })
        self.assertEqual(parsed.symbol, "EURUSD")
        self.assertEqual(parsed.account, "1345657215")

    def test_side_aliases(self):
        for value in ("buy", "LONG", "bullish", "crossing_up", "above", "BUY_LIMIT"):
            with self.subTest(value=value):
                self.assertEqual(signals.parse_side(value), "BUY")
        for value in ("sell", "SHORT", "bearish", "crossing_down", "below", "SELL_STOP"):
            with self.subTest(value=value):
                self.assertEqual(signals.parse_side(value), "SELL")
        for value in ("price alert", "webhook", "buy and sell", ""):
            with self.subTest(value=value):
                self.assertIsNone(signals.parse_side(value))

    def test_number_formats(self):
        cases = {
            "1.0800": 1.08,
            "1,0800": 1.08,
            "SL: 1.0800": 1.08,
            "1,085.50": 1085.5,
            "39500": 39500.0,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertAlmostEqual(signals.parse_number(raw), expected)

    def test_message_with_account(self):
        parsed = signals.parse_signal(
            "SELL GBP/USD stop loss 1.2700 account=1345657215",
            known_symbols=KNOWN,
        )
        self.assertEqual(parsed.account, "1345657215")
        self.assertEqual(parsed.side, "SELL")
        self.assertEqual(parsed.symbol, "GBPUSD")
        self.assertAlmostEqual(parsed.sl, 1.27)

    def test_known_index_in_text(self):
        parsed = signals.parse_signal(
            "BUY US30 SL=39500", known_symbols=KNOWN
        )
        self.assertEqual(parsed.symbol, "US30")
        self.assertEqual(parsed.side, "BUY")

    def test_unknown_symbol_is_not_invented(self):
        parsed = signals.parse_signal("BUY FOOBAR SL=1.0", known_symbols=KNOWN)
        self.assertIsNone(parsed.symbol)
        self.assertIn("symbol", parsed.missing)

    def test_conflicting_direction_is_flagged(self):
        parsed = signals.parse_signal({
            "ticker": "EURUSD",
            "side": "buy",
            "sl": 1.08,
            "message": "SELL EURUSD SL=1.08",
        })
        self.assertEqual(parsed.side, "BUY")
        self.assertTrue(parsed.conflicts)
        self.assertFalse(parsed.ok)

    def test_missing_sl(self):
        parsed = signals.parse_signal({"symbol": "EURUSD", "side": "buy"})
        self.assertEqual(parsed.missing, ["sl"])
        self.assertFalse(parsed.ok)

    def test_nested_json_string_payload(self):
        inner = {
            "symbol": "EURUSD",
            "side": "sell",
            "sl": 1.09,
            "message": "SELL EURUSD SL=1.09",
        }
        parsed = signals.parse_signal({"payload": json.dumps(inner)})
        self.assertEqual(parsed.symbol, "EURUSD")
        self.assertEqual(parsed.side, "SELL")
        self.assertAlmostEqual(parsed.sl, 1.09)

    def test_event_key_is_stable_and_content_sensitive(self):
        first = signals.parse_signal("BUY EURUSD SL=1.0800")
        same = signals.parse_signal("BUY EURUSD SL=1.0800")
        different = signals.parse_signal("BUY EURUSD SL=1.0900")
        self.assertEqual(first.event_key("1345657215"), same.event_key("1345657215"))
        self.assertNotEqual(first.event_key("1345657215"), different.event_key("1345657215"))

    def test_event_key_ignores_alert_id(self):
        first = signals.parse_signal(
            {"id": "alert-1", "symbol": "EURUSD", "side": "buy", "sl": 1.08}
        )
        second = signals.parse_signal(
            {"id": "alert-2", "symbol": "EURUSD", "side": "buy", "sl": 1.08}
        )
        self.assertEqual(first.event_key("1"), second.event_key("1"))


class SignalRequestTests(unittest.TestCase):
    def test_decode_json(self):
        data = signals.decode_body(b'{"symbol":"EURUSD"}', "application/json")
        self.assertEqual(data, {"symbol": "EURUSD"})

    def test_decode_form(self):
        data = signals.decode_body(
            b"symbol=EURUSD&side=buy&sl=1.08",
            "application/x-www-form-urlencoded",
        )
        self.assertEqual(data["symbol"], "EURUSD")
        self.assertEqual(data["side"], "buy")

    def test_decode_plain_text(self):
        data = signals.decode_body(b"BUY EURUSD SL=1.08", "text/plain")
        self.assertEqual(data, {"message": "BUY EURUSD SL=1.08"})

    def test_decode_invalid_json_falls_back_to_text(self):
        data = signals.decode_body(b"BUY EURUSD SL=1.08", "application/json")
        self.assertEqual(data, {"message": "BUY EURUSD SL=1.08"})

    def test_extract_token_sources(self):
        self.assertEqual(
            signals.extract_token({}, query={"token": "query-secret"}), "query-secret"
        )
        self.assertEqual(
            signals.extract_token({}, headers={"Authorization": "Bearer header-secret"}),
            "header-secret",
        )
        self.assertEqual(
            signals.extract_token({"signal_token": "json-secret"}), "json-secret"
        )
        self.assertEqual(
            signals.extract_token({"message": "BUY EURUSD SL=1.08 TOKEN=msg-secret"}),
            "msg-secret",
        )

    def test_redaction_removes_secrets(self):
        payload = {
            "token": "top-secret-value",
            "message": "BUY EURUSD SL=1.08 TOKEN=top-secret-value",
            "symbol": "EURUSD",
        }
        redacted = signals.redact_payload(payload, ["top-secret-value"])
        blob = json.dumps(redacted)
        self.assertNotIn("top-secret-value", blob)
        self.assertEqual(redacted["symbol"], "EURUSD")


class SignalStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmpdir.name, "test.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.tmpdir.cleanup()

    def test_claim_dedupes_within_window_and_allows_after(self):
        def claim(ts):
            return db.claim_signal(
                "event-key",
                ts,
                login="1345657215",
                symbol="EURUSD",
                side="BUY",
                sl=1.08,
                message="BUY EURUSD SL=1.08",
                raw="{}",
                window_seconds=300,
            )

        self.assertTrue(claim(1000.0))
        self.assertFalse(claim(1299.0))
        self.assertTrue(claim(1300.0))
        rows = db.recent_signals(10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "received")

    def test_update_signal_status(self):
        db.claim_signal("event-key", 1000.0, login="1", symbol="EURUSD",
                        side="BUY", sl=1.08)
        db.update_signal("event-key", "queued")
        self.assertEqual(db.recent_signals(1)[0]["status"], "queued")
        db.update_signal("event-key", "rejected", "blocked")
        row = db.recent_signals(1)[0]
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["note"], "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)

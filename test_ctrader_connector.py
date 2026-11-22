"""Dev-only tests for the cTrader connector. No network or credentials needed."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from ctrader_connector import protocol
from ctrader_connector.account import (
    AccountWorker,
    classify_close,
    execution_name,
    net_close_profit,
    order_plan,
    order_status_name,
    order_type_name,
    parse_command,
    pending_price,
    pending_type_label,
    position_status_name,
    account_type_name,
    side_name,
)
from ctrader_connector.symbols import (
    LightSymbol,
    SizingError,
    SymbolBook,
    SymbolMeta,
    conversion_rate,
    fmt_price,
    lots_from_volume,
    money,
    price,
    quote_key,
    round_price,
    size_order,
)


class FrameTests(unittest.TestCase):
    def test_roundtrip(self):
        message = {"clientMsgId": "abc", "payloadType": 2100,
                   "payload": {"clientId": "x", "clientSecret": "y"}}
        frame = protocol.encode_frame(message)
        self.assertTrue(frame.endswith(b"\n"))
        decoded = protocol.decode_frame(frame[:-1])
        self.assertEqual(decoded, message)

    def test_decode_available_handles_concatenated_and_partial(self):
        first = '{"payloadType":2101,"clientMsgId":"a"}'
        second = '{"payloadType":50,"clientMsgId":"b"}'
        messages, rest = protocol.decode_available(first + "\n" + second + "\n{")
        self.assertEqual([m["payloadType"] for m in messages], [2101, 50])
        self.assertEqual(rest.strip(), "{")

    def test_decode_available_keeps_incomplete_object(self):
        messages, rest = protocol.decode_available('{"payloadType":21')
        self.assertEqual(messages, [])
        self.assertEqual(rest, '{"payloadType":21')

    def test_error_message(self):
        error = protocol.error_from_message({
            "payloadType": protocol.OA_ERROR_RES,
            "payload": {"errorCode": "OA_AUTH_TOKEN_EXPIRED", "description": "expired"},
        })
        self.assertIsNotNone(error)
        self.assertEqual(error.error_code, "OA_AUTH_TOKEN_EXPIRED")
        self.assertIsNone(protocol.error_from_message({"payloadType": 2101}))


class EnumTests(unittest.TestCase):
    def test_names_and_numbers(self):
        self.assertEqual(execution_name("ORDER_FILLED"), "ORDER_FILLED")
        self.assertEqual(execution_name(3), "ORDER_FILLED")
        self.assertEqual(execution_name("order_filled"), "ORDER_FILLED")
        self.assertEqual(order_status_name(2), "ORDER_STATUS_FILLED")
        self.assertEqual(order_status_name("ORDER_STATUS_ACCEPTED"), "ORDER_STATUS_ACCEPTED")
        self.assertEqual(order_status_name("2"), "ORDER_STATUS_FILLED")
        self.assertEqual(execution_name("3"), "ORDER_FILLED")
        self.assertEqual(account_type_name("1"), "NETTED")
        self.assertEqual(order_type_name(2), "LIMIT")
        self.assertEqual(position_status_name(1), "POSITION_STATUS_OPEN")
        self.assertEqual(side_name(1), "BUY")
        self.assertEqual(side_name("SELL"), "SELL")
        self.assertEqual(account_type_name(1), "NETTED")
        self.assertEqual(account_type_name("HEDGED"), "HEDGED")
        self.assertEqual(side_name(99), "")


class CommandTests(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(parse_command("SYMBOLS EURUSD,XAUUSD"), ("SYMBOLS", ["EURUSD,XAUUSD"]))

    def test_scalars(self):
        self.assertEqual(parse_command("RISK 1.5"), ("RISK", ["1.5"]))
        self.assertEqual(parse_command("MAXPOS 2"), ("MAXPOS", ["2"]))
        self.assertEqual(parse_command("RRR 2"), ("RRR", ["2"]))

    def test_orders(self):
        name, args = parse_command("OPEN BUY EURUSD 1.1 1.09 1.12 1.0")
        self.assertEqual(name, "OPEN")
        self.assertEqual(args, ["BUY", "EURUSD", "1.1", "1.09", "1.12", "1.0"])
        name, args = parse_command("OPEN_MARKET SELL XAUUSD 2400 0.5 2")
        self.assertEqual(name, "OPEN_MARKET")
        self.assertEqual(args, ["SELL", "XAUUSD", "2400", "0.5", "2"])

    def test_closes_and_unknowns(self):
        self.assertEqual(parse_command("CLOSE 123"), ("CLOSE", ["123"]))
        self.assertEqual(parse_command("CLOSE_ALL"), ("CLOSE_ALL", []))
        self.assertEqual(parse_command("DELETE_PENDING 9"), ("DELETE_PENDING", ["9"]))
        self.assertEqual(parse_command("BLOCK"), ("BLOCK", []))
        self.assertIsNone(parse_command("SOMETHING_NEW 1"))
        self.assertIsNone(parse_command(""))
        self.assertIsNone(parse_command("OPEN BUY EURUSD 1"))


class SymbolTests(unittest.TestCase):
    def _book(self):
        book = SymbolBook({"US100": "US100.cash"})
        book.add_light({"symbolId": 1, "symbolName": "EUR/USD", "enabled": True,
                        "baseAssetId": 1, "quoteAssetId": 2})
        book.add_light({"symbolId": 2, "symbolName": "USD/JPY", "enabled": True,
                        "baseAssetId": 2, "quoteAssetId": 3})
        book.add_light({"symbolId": 3, "symbolName": "US100.cash", "enabled": True,
                        "baseAssetId": 10, "quoteAssetId": 2})
        book.add_light({"symbolId": 4, "symbolName": "EURUSD.raw", "enabled": True,
                        "baseAssetId": 1, "quoteAssetId": 2})
        book.load_meta([
            {"symbolId": 1, "symbolName": "EUR/USD", "digits": 5, "lotSize": 10_000_000,
             "minVolume": 100_000, "stepVolume": 100_000, "maxVolume": 100_000_000},
        ])
        return book

    def test_exact_and_alias(self):
        book = self._book()
        self.assertEqual(book.resolve("eurusd").light.symbol_id, 1)
        self.assertEqual(book.resolve("US100").light.symbol_id, 3)
        self.assertEqual(book.resolve("us100.cash").light.symbol_id, 3)

    def test_ambiguous_suffix_is_rejected(self):
        book = SymbolBook()
        book.add_light({"symbolId": 3, "symbolName": "US100.cash", "enabled": True,
                        "baseAssetId": 10, "quoteAssetId": 2})
        book.add_light({"symbolId": 5, "symbolName": "US100.raw", "enabled": True,
                        "baseAssetId": 10, "quoteAssetId": 2})
        self.assertIsNone(book.resolve("US100"))

    def test_price_and_money_helpers(self):
        self.assertEqual(price("123456"), 1.23456)
        self.assertIsNone(price(None))
        self.assertEqual(money("1029718", 2), 10297.18)
        self.assertEqual(lots_from_volume(10_000_000, 10_000_000), 1.0)
        self.assertEqual(fmt_price(1.234567, 5), "1.23457")
        self.assertEqual(quote_key("US100.cash"), "us100_cash")

    def test_round_price_matches_symbol_digits(self):
        # The mobile ticket can send 10 decimals; BTCUSD only allows 2.
        self.assertEqual(round_price(61361.2182188867, 2), 61361.22)
        self.assertEqual(round_price(1.23456789, 5), 1.23457)
        self.assertEqual(round_price(0.0, 2), 0.0)
        self.assertEqual(round_price(None, 2), 0.0)

    def test_conversion_uses_conservative_side(self):
        book = self._book()
        quotes = {1: {"bid": 1.10, "ask": 1.20}, 2: {"bid": 150.0, "ask": 151.0}}
        edges = book.edges()
        # USD -> EUR: divide by bid (not ask) so risk looks larger.
        self.assertAlmostEqual(conversion_rate(2, 1, edges, quotes), 1 / 1.10)
        # EUR -> USD: multiply by ask.
        self.assertAlmostEqual(conversion_rate(1, 2, edges, quotes), 1.20)
        # EUR -> JPY via USD/JPY, highest path wins.
        self.assertAlmostEqual(conversion_rate(1, 3, edges, quotes), 1.20 * 151.0)
        self.assertIsNone(conversion_rate(1, 99, edges, quotes))


class SizingTests(unittest.TestCase):
    def test_basic_risk_sizing(self):
        sizing = size_order(
            balance=10_000, risk_pct=1.0, entry=1.2000, stop_loss=1.1900,
            lot_size=10_000_000, min_volume=100_000, step_volume=100_000,
            max_volume=0, quote_to_deposit=1.0,
        )
        self.assertEqual(sizing.volume, 1_000_000)
        self.assertAlmostEqual(sizing.lots, 0.10)
        self.assertAlmostEqual(sizing.risk_per_lot, 1_000.0)
        self.assertAlmostEqual(sizing.actual_risk, 100.0)
        self.assertFalse(sizing.margin_capped)

    def test_conversion_increases_risk(self):
        sizing = size_order(
            balance=10_000, risk_pct=1.0, entry=1.2000, stop_loss=1.1900,
            lot_size=10_000_000, min_volume=100_000, step_volume=100_000,
            max_volume=0, quote_to_deposit=1.20,
        )
        self.assertAlmostEqual(sizing.risk_per_lot, 1_200.0)
        self.assertAlmostEqual(sizing.lots, 0.08)
        self.assertEqual(sizing.volume, 800_000)

    def test_margin_cap(self):
        sizing = size_order(
            balance=10_000, risk_pct=1.0, entry=1.2000, stop_loss=1.1900,
            lot_size=10_000_000, min_volume=100_000, step_volume=100_000,
            max_volume=0, quote_to_deposit=1.0,
            margin_per_lot=2_000, equity=10_000, margin_used=0, margin_cap_pct=80,
        )
        self.assertFalse(sizing.margin_capped)
        self.assertAlmostEqual(sizing.margin_allows_lots, 4.0)

    def test_margin_cap_below_min_lot(self):
        with self.assertRaises(SizingError) as ctx:
            size_order(
                balance=10_000, risk_pct=1.0, entry=1.2000, stop_loss=1.1900,
                lot_size=10_000_000, min_volume=100_000, step_volume=100_000,
                max_volume=0, quote_to_deposit=1.0,
                margin_per_lot=1_000_000, equity=10_000, margin_used=0,
                margin_cap_pct=80,
            )
        self.assertEqual(ctx.exception.reason, "margin_cap_below_min_lot")
        self.assertAlmostEqual(ctx.exception.details["margin_allows_lots"], 0.01)

    def test_missing_sl(self):
        with self.assertRaises(SizingError) as ctx:
            size_order(
                balance=10_000, risk_pct=1.0, entry=1.2, stop_loss=1.2,
                lot_size=10_000_000, min_volume=100_000, step_volume=100_000,
                max_volume=0, quote_to_deposit=1.0,
            )
        self.assertEqual(ctx.exception.reason, "missing_sl")


class TradeHelperTests(unittest.TestCase):
    def test_net_close_profit(self):
        detail = {"grossProfit": 1500, "swap": -100, "commission": -200,
                  "pnlConversionFee": -50, "moneyDigits": 2}
        self.assertAlmostEqual(net_close_profit(detail), 11.50)
        without_digits = {k: v for k, v in detail.items() if k != "moneyDigits"}
        self.assertAlmostEqual(net_close_profit(without_digits, fallback_digits=3), 1.15)

    def test_order_plan(self):
        tol = 0.001
        self.assertEqual(order_plan("BUY", 1.1000, 1.1005, 1.1000, tol), "MARKET")
        self.assertEqual(order_plan("BUY", 1.0900, 1.1005, 1.1000, tol), "LIMIT")
        self.assertEqual(order_plan("BUY", 1.1100, 1.1005, 1.1000, tol), "STOP")
        self.assertEqual(order_plan("SELL", 1.1000, 1.1005, 1.1000, tol), "MARKET")
        self.assertEqual(order_plan("SELL", 1.1100, 1.1005, 1.1000, tol), "LIMIT")
        self.assertEqual(order_plan("SELL", 1.0900, 1.1005, 1.1000, tol), "STOP")
        self.assertEqual(order_plan("BUY", 1.1, 0, 0, tol), "")

    def test_classify_close(self):
        position = {"stopLoss": 1.1900, "takeProfit": 1.2100,
                    "tradeData": {"tradeSide": "BUY"}}
        self.assertEqual(classify_close({"executionPrice": 1.1899}, position, 5), "SL")
        self.assertEqual(classify_close({"executionPrice": 1.2101}, position, 5), "TP")
        self.assertEqual(classify_close({"executionPrice": 1.2000}, position, 5), "MANUAL")
        self.assertEqual(classify_close({"executionPrice": 1.2000}, position, 5, True), "EA")
        sell = {"stopLoss": 1.2100, "takeProfit": 1.1900,
                "tradeData": {"tradeSide": "SELL"}}
        self.assertEqual(classify_close({"executionPrice": 1.2101}, sell, 5), "SL")
        self.assertEqual(classify_close({"executionPrice": 1.1899}, sell, 5), "TP")

    def test_pending_labels(self):
        order = {"orderType": "LIMIT", "limitPrice": 1.09,
                 "tradeData": {"tradeSide": 1}}
        self.assertEqual(pending_type_label(order), "BUY_LIMIT")
        self.assertAlmostEqual(pending_price(order), 1.09)
        order = {"orderType": 3, "stopPrice": 1.11, "tradeData": {"tradeSide": "SELL"}}
        self.assertEqual(pending_type_label(order), "SELL_STOP")
        self.assertAlmostEqual(pending_price(order), 1.11)


class _DummyBackend:
    async def post_report(self, login, fields):
        self.report = (login, fields)

    async def post_event(self, login, message):
        pass

    async def get_commands(self, login):
        return []


def _worker(tmpdir: str, account_type: str = "") -> AccountWorker:
    settings = SimpleNamespace(
        state_dir=Path(tmpdir),
        symbol_aliases={},
        client_id="id",
        client_secret="secret",
        port=5036,
        connect_timeout=5,
        request_timeout=5,
        margin_cap_pct=80,
        command_interval=1,
        report_interval=2,
        reconcile_interval=15,
        price_tolerance_points=10,
        host_for=lambda env: "demo.ctraderapi.com",
    )
    account = {
        "login": "ctrader:111",
        "external_id": "111",
        "grant_id": 7,
        "environment": "demo",
        "account_type": account_type,
        "symbols": ["EURUSD"],
        "risk_pct": 1.0,
    }
    return AccountWorker(settings, _DummyBackend(), 7, account)


class FillAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_fills_accumulate_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _worker(tmp)

            def event(deal_id, filled, executed, position_volume):
                return {
                    "payload": {
                        "executionType": "ORDER_PARTIAL_FILL",
                        "order": {
                            "orderId": 10,
                            "orderType": "MARKET",
                            "orderStatus": "ORDER_ACCEPTED",
                            "executedVolume": executed,
                            "tradeData": {
                                "symbolId": 1, "tradeSide": "BUY",
                                "volume": 10_000_000, "label": "GuardRails",
                            },
                        },
                        "deal": {
                            "dealId": deal_id, "positionId": 99,
                            "filledVolume": filled,
                        },
                        "position": {
                            "positionId": 99,
                            "positionStatus": "POSITION_STATUS_OPEN",
                            "tradeData": {
                                "symbolId": 1, "tradeSide": "BUY",
                                "volume": position_volume,
                                "label": "GuardRails",
                                "openTimestamp": 1_700_000_000_000,
                            },
                        },
                    }
                }

            await worker._handle_execution(event(500, 4_000_000, 4_000_000, 4_000_000)["payload"])
            self.assertEqual(worker.persisted[99]["bot_volume"], 4_000_000)
            await worker._handle_execution(event(501, 6_000_000, 10_000_000, 10_000_000)["payload"])
            self.assertEqual(worker.persisted[99]["bot_volume"], 10_000_000)
            # Replaying the second execution event must not double-count.
            await worker._handle_execution(event(501, 6_000_000, 10_000_000, 10_000_000)["payload"])
            self.assertEqual(worker.persisted[99]["bot_volume"], 10_000_000)


class ReportTests(unittest.TestCase):
    def _prepare(self, worker):
        worker.symbols = ["EURUSD"]
        worker.book.add_light({"symbolId": 1, "symbolName": "EUR/USD", "enabled": True,
                               "baseAssetId": 1, "quoteAssetId": 2})
        worker.book.load_meta([{
            "symbolId": 1, "symbolName": "EUR/USD", "digits": 5,
            "lotSize": 10_000_000, "minVolume": 100_000, "stepVolume": 100_000,
            "maxVolume": 100_000_000, "baseAssetId": 1, "quoteAssetId": 2,
        }])
        worker.book.bind_root("EURUSD", 1)
        worker.resolved["EURUSD"] = worker.book.resolve("EURUSD")
        worker.roots_by_id[1] = "EURUSD"
        worker.trader = {"balance": 100_000, "moneyDigits": 2, "depositAssetId": 2}
        worker.money_digits = 2
        worker.quotes = {1: {"bid": 1.10000, "ask": 1.10020, "ts": time.monotonic()}}

    def test_report_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _worker(tmp)
            self._prepare(worker)
            worker.positions = {123: {
                "positionId": 123,
                "price": 1.10,
                "stopLoss": 1.1000,
                "takeProfit": 1.3000,
                "usedMargin": 1000,
                "tradeData": {"symbolId": 1, "volume": 10_000_000,
                              "tradeSide": "BUY", "label": "GuardRails"},
            }}
            worker.bot_positions = {123}
            worker.pnl = {123: 25.0}
            worker.orders = {456: {
                "orderId": 456,
                "orderType": "LIMIT",
                "limitPrice": 1.09,
                "stopLoss": 1.08,
                "takeProfit": 1.12,
                "tradeData": {"symbolId": 1, "volume": 10_000_000,
                              "tradeSide": "BUY", "label": "GuardRails"},
            }}
            worker.bot_orders = {456}

            fields = worker._report_fields()
            self.assertEqual(fields["balance"], "1000.00")
            self.assertEqual(fields["equity"], "1025.00")
            self.assertEqual(fields["margin"], "10.00")
            self.assertEqual(fields["freemargin"], "1015.00")
            self.assertEqual(fields["blocked"], "0")
            self.assertEqual(fields["eurusd_ask"], "1.10020")
            self.assertEqual(fields["eurusd_bid"], "1.10000")
            self.assertEqual(
                fields["positions"], "EURUSD,123,BUY,1.00,25.00,1.10000,1.30000"
            )
            self.assertEqual(
                fields["pending"], "EURUSD,456,BUY_LIMIT,1.09000,1.08000,1.12000"
            )

    def test_stale_quote_reports_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _worker(tmp)
            self._prepare(worker)
            worker.quotes[1]["ts"] = time.monotonic() - 1000
            fields = worker._report_fields()
            self.assertEqual(fields["eurusd_ask"], "0")
            self.assertEqual(fields["eurusd_bid"], "0")

    def test_netted_pnl_is_prorated(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _worker(tmp, account_type="NETTED")
            self._prepare(worker)
            worker.positions = {123: {
                "positionId": 123, "price": 1.10, "stopLoss": 1.0,
                "takeProfit": 1.3, "usedMargin": 2000,
                "tradeData": {"symbolId": 1, "volume": 10_000_000,
                              "tradeSide": "BUY", "label": "GuardRails"},
            }}
            worker.bot_positions = {123}
            worker.persisted[123] = {
                "symbol_id": 1, "root": "EURUSD", "side": "BUY",
                "volume": 10_000_000, "bot_volume": 5_000_000,
                "open_ts": 0, "sl": 1.0, "tp": 1.3,
            }
            worker.pnl = {123: 40.0}

            fields = worker._report_fields()
            self.assertEqual(
                fields["positions"], "EURUSD,123,BUY,0.50,20.00,1.00000,1.30000"
            )

    def test_netted_conflict_blocks_opposite_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _worker(tmp, account_type="NETTED")
            worker.positions[5] = {
                "tradeData": {"symbolId": 1, "tradeSide": "BUY", "volume": 1_000_000}
            }
            self.assertTrue(worker._netted_conflict(1, "SELL"))
            self.assertFalse(worker._netted_conflict(1, "BUY"))
            worker.account_type = "HEDGED"
            self.assertFalse(worker._netted_conflict(1, "SELL"))

    def test_pending_fill_status_is_not_removed(self):
        self.assertEqual(order_status_name(6), "ORDER_STATUS_PARTIALLY_FILLED")

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker = _worker(tmp)
            worker.persisted[123] = {"symbol_id": 1, "root": "EURUSD", "side": "BUY",
                                     "volume": 10_000_000, "bot_volume": 10_000_000,
                                     "open_ts": 1, "sl": 1.0, "tp": 1.3}
            worker.closed_reported.add(456)
            worker._persist_state()
            reloaded = _worker(tmp)
            self.assertEqual(reloaded.persisted[123]["root"], "EURUSD")
            self.assertIn(456, reloaded.closed_reported)
            raw = json.loads((Path(tmp) / "ctrader_111.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["ctid_trader_account_id"], 111)


if __name__ == "__main__":
    unittest.main()

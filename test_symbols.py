"""Dev-only checks for the Discord-managed symbol list."""

import unittest

import symbols as sym


class SymbolTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(sym.normalize_symbol(" eurusd "), "EURUSD")
        self.assertEqual(sym.normalize_symbol("XAU/USD"), "XAUUSD")
        self.assertEqual(sym.normalize_symbol("xauusd"), "XAUUSD")

    def test_valid_roots(self):
        for raw in ("EURUSD", "XAUUSD", "US30", "US30#", "EURUSD.m", "BTC_USD"):
            value, error = sym.validate_symbol(raw)
            self.assertIsNone(error, raw)
            self.assertEqual(value, sym.normalize_symbol(raw))

    def test_invalid_roots(self):
        for raw in ("", "  ", "EU USD", "EUR/USD extra", "A", "X" * 30, "EUR$USD"):
            value, error = sym.validate_symbol(raw)
            self.assertIsNone(value, raw)
            self.assertIsNotNone(error, raw)

    def test_parse_list_dedupes_and_reports_errors(self):
        valid, errors = sym.parse_symbol_input("EURUSD, XAUUSD, eurusd, ???")
        self.assertEqual(valid, ["EURUSD", "XAUUSD"])
        self.assertEqual(len(errors), 1)

    def test_parse_list_accepts_slash(self):
        valid, errors = sym.parse_symbol_input("XAU/USD,GBP/USD")
        self.assertEqual(valid, ["XAUUSD", "GBPUSD"])
        self.assertEqual(errors, [])

    def test_quote_key_matches_ea_format(self):
        self.assertEqual(sym.quote_key("EURUSD"), "eurusd")
        self.assertEqual(sym.quote_key("EURUSD.m"), "eurusd_m")
        self.assertEqual(sym.quote_key("US30#"), "us30_")

    def test_merge_dedupes(self):
        merged, added, already, errors = sym.merge_symbols(
            ["EURUSD"], ["EURUSD", "XAUUSD"]
        )
        self.assertEqual(merged, ["EURUSD", "XAUUSD"])
        self.assertEqual(added, ["XAUUSD"])
        self.assertEqual(already, ["EURUSD"])
        self.assertEqual(errors, [])

    def test_merge_respects_cap(self):
        existing = [f"S{i}" for i in range(sym.MAX_SYMBOLS_PER_ACCOUNT)]
        merged, added, already, errors = sym.merge_symbols(existing, ["XAUUSD"])
        self.assertEqual(merged, existing)
        self.assertEqual(added, [])
        self.assertEqual(len(errors), 1)

    def test_at_least_one_symbol_can_be_kept(self):
        # Removing the last symbol is rejected by the command, so an empty
        # list always means "not configured yet" and can be seeded safely.
        self.assertTrue(sym.DEFAULT_SYMBOLS)
        self.assertGreater(len(sym.DEFAULT_SYMBOLS), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

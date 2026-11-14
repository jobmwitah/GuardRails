"""Tests for the MetaEq Manager catalog and control file."""

import unittest

import metaeq


class CatalogTests(unittest.TestCase):
    def test_every_strategy_has_exactly_one_pair(self):
        seen = {}
        for name, strategy in metaeq.STRATEGIES.items():
            self.assertNotIn(name, seen)
            seen[name] = strategy.pair
            self.assertIn(strategy.pair, metaeq.PAIR_ORDER)

    def test_pair_membership_matches_verified_set(self):
        self.assertEqual(metaeq.pair_strategies("NAS100"), ("S1", "S3", "S4", "S8", "S9"))
        self.assertEqual(metaeq.pair_strategies("XAUUSD"), ("S2B", "S6"))
        self.assertEqual(metaeq.pair_strategies("USDJPY"), ("S2A", "S3B", "S5", "S7"))

    def test_ranging_split(self):
        self.assertEqual(metaeq.affected_strategies("NAS100"), ("S1", "S3", "S4", "S8"))
        self.assertEqual(metaeq.non_affected_strategies("NAS100"), ("S9",))
        self.assertEqual(metaeq.affected_strategies("XAUUSD"), ("S2B", "S6"))
        self.assertEqual(metaeq.non_affected_strategies("XAUUSD"), ())
        self.assertEqual(metaeq.affected_strategies("USDJPY"), ("S2A", "S3B", "S5"))
        self.assertEqual(metaeq.non_affected_strategies("USDJPY"), ("S7",))

    def test_strategy_lookup_is_case_insensitive_but_canonical(self):
        self.assertEqual(metaeq.normalize_strategy("s3b"), "S3B")
        self.assertEqual(metaeq.normalize_pair("xauusd"), "XAUUSD")
        self.assertIsNone(metaeq.normalize_strategy("S10"))

    def test_symbol_suffix_preserves_casing(self):
        self.assertEqual(metaeq.resolve_symbol("NAS100"), "NAS100")
        self.assertEqual(metaeq.resolve_symbol("XAUUSD", ".a"), "XAUUSD.a")
        self.assertEqual(metaeq.resolve_symbol("USDJPY", "0"), "USDJPY")

    def test_magic_offsets_are_inside_guardrails_exemption(self):
        for offset in metaeq.MAGIC_OFFSETS.values():
            magic = 100000 + offset
            self.assertGreaterEqual(magic, 100000)
            self.assertLessEqual(magic, 119999)


class ControlFileTests(unittest.TestCase):
    def test_round_trip(self):
        states = metaeq.default_states()
        states["S4"] = False
        states["S7"] = False
        text = metaeq.control_text("61603648", states, "NAS100.a")
        parsed = metaeq.parse_control_text(text)
        self.assertTrue(parsed["S1"])
        self.assertFalse(parsed["S4"])
        self.assertFalse(parsed["S7"])

    def test_missing_version_fails_closed(self):
        text = "S1=1\r\nS2A=1\r\n"
        parsed = metaeq.parse_control_text(text)
        self.assertFalse(any(parsed.values()))

    def test_corrupt_version_fails_closed(self):
        text = "version=2\r\nS1=1\r\n"
        parsed = metaeq.parse_control_text(text)
        self.assertFalse(any(parsed.values()))


if __name__ == "__main__":
    unittest.main()

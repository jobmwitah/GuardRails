"""Tests for MetaEq preset/template generation in the Windows agent."""

import tempfile
import unittest
from pathlib import Path

from mt5_agent import read_set_values, render_metaeq_template


REPO = Path(__file__).resolve().parent.parent


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.values = read_set_values(REPO / "MetaEq.set")
        self.symbols = {
            "NAS100": "NAS100.a",
            "XAUUSD": "XAUUSD.a",
            "USDJPY": "USDJPY.a",
        }

    def test_preset_has_all_strategies(self):
        for name in ("S1", "S2A", "S2B", "S3", "S3B", "S4", "S5", "S6", "S7", "S8", "S9"):
            self.assertIn(f"{name}_Enabled", self.values)

    def test_template_pins_symbols_and_enables_initialization(self):
        text = render_metaeq_template(self.values, self.symbols, "NAS100.a")
        self.assertIn("symbol=NAS100.a", text)
        self.assertIn("name=MetaEq", text)
        self.assertIn("path=Experts\\MetaEq.ex5", text)
        self.assertIn("S1_Symbols=NAS100.a", text)
        self.assertIn("S2A_Symbols=USDJPY.a", text)
        self.assertIn("S2B_Symbols=XAUUSD.a", text)
        self.assertIn("S3_Symbols=NAS100.a", text)
        self.assertIn("S3B_Symbols=USDJPY.a", text)
        self.assertIn("S4_Symbol=CURRENT", text)
        self.assertIn("S5_Symbol=USDJPY.a", text)
        self.assertIn("S6_Symbol=XAUUSD.a", text)
        self.assertIn("S7_Symbol=USDJPY.a", text)
        self.assertIn("S8_Symbol=NAS100.a", text)
        self.assertIn("S9_Symbols=NAS100.a", text)
        for name in ("S1", "S2A", "S2B", "S3", "S3B", "S4", "S5", "S6", "S7", "S8", "S9"):
            self.assertIn(f"{name}_Enabled=true", text)

    def test_template_is_parseable_set_data(self):
        text = render_metaeq_template(self.values, self.symbols, "NAS100.a")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "MetaEq.set"
            path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
            parsed = read_set_values(path)
        self.assertEqual(parsed["GlobalBaseMagic"], "100000")
        self.assertEqual(parsed["S3_Symbols"], "NAS100.a")


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the pure MT5 provisioning helpers."""

import tempfile
import unittest
from pathlib import Path

import mt5_provisioning as mp


class ValidationTests(unittest.TestCase):
    def test_login(self):
        self.assertEqual(mp.validate_login(" 61603648 "), "61603648")
        for bad in ("", "abc", "123", "1234567890123", "12 34"):
            with self.assertRaises(ValueError):
                mp.validate_login(bad)

    def test_server(self):
        self.assertEqual(mp.validate_server(" Pepperstone-Demo "), "Pepperstone-Demo")
        for bad in ("", "x" * 121, "bad\nserver", "bad\0server"):
            with self.assertRaises(ValueError):
                mp.validate_server(bad)

    def test_password(self):
        self.assertEqual(mp.validate_password(" s3cret "), " s3cret ")
        for bad in ("", "x" * 129, "bad\npassword", "bad\0password"):
            with self.assertRaises(ValueError):
                mp.validate_password(bad)

    def test_broker_url(self):
        good = "https://download.mql5.com/cdn/web/five.percent.online/mt5/setup.exe"
        self.assertEqual(mp.validate_broker_url(good), good)
        self.assertEqual(mp.validate_broker_url("  "), "")
        for bad in (
            "http://download.mql5.com/setup.exe",
            "https://download.mql5.com/setup.msi",
            "https://localhost/setup.exe",
            "https://127.0.0.1/setup.exe",
            "https://10.0.0.5/setup.exe",
            "file:///tmp/setup.exe",
        ):
            with self.assertRaises(ValueError):
                mp.validate_broker_url(bad)


class IniTests(unittest.TestCase):
    def test_round_trip_utf16(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "start.ini"
            mp.write_ini(path, ["[Common]", "Login=1", "Server=Demo"])
            self.assertEqual(
                mp.read_ini(path), ["[Common]", "Login=1", "Server=Demo"]
            )

    def test_set_key_adds_and_replaces_case_insensitively(self):
        lines = ["[Common]", "login=old", "Server=Demo", "[Experts]", "Enabled=0"]
        lines = mp.set_key(lines, "[Common]", "Login", "new")
        lines = mp.set_key(lines, "[Experts]", "Enabled", "1")
        self.assertIn("Login=new", lines)
        self.assertIn("Enabled=1", lines)
        self.assertNotIn("login=old", lines)

    def test_scrub_password_only_in_common(self):
        lines = [
            "[Common]",
            "Login=1",
            "Password=secret",
            "ProxyPassword=proxy",
            "[Experts]",
            "Password=not-a-credential",
        ]
        cleaned = mp.scrub_password_lines(lines)
        self.assertNotIn("Password=secret", cleaned)
        self.assertNotIn("ProxyPassword=proxy", cleaned)
        self.assertIn("Password=not-a-credential", cleaned)

    def test_one_time_password_inserted_after_login(self):
        lines = ["[Common]", "Login=1", "Server=Demo"]
        out = mp.with_one_time_password(lines, "secret")
        self.assertEqual(out[1:4], ["Login=1", "Password=secret", "Server=Demo"])

    def test_render_start_ini_has_fixed_expert_settings(self):
        base = ["[Common]", "Login=", "Server=", "[Experts]", "[StartUp]"]
        lines = mp.render_start_ini(
            base, login="61603648", server="Pepperstone-Demo", password="secret"
        )
        text = "\n".join(lines)
        self.assertIn("Login=61603648", text)
        self.assertIn("Server=Pepperstone-Demo", text)
        self.assertIn("Password=secret", text)
        self.assertIn("WebRequestUrl=http://127.0.0.1:8000", text)
        self.assertIn("AllowLiveTrading=1", text)
        self.assertIn("AllowDllImport=0", text)
        self.assertIn("Expert=EA_Bridge", text)
        self.assertIn("ExpertParameters=EA_Bridge.set", text)

    def test_render_start_ini_without_password_scrubs_it(self):
        base = ["[Common]", "Login=1", "Password=old", "[Experts]", "[StartUp]"]
        text = "\n".join(
            mp.render_start_ini(base, login="1", server="Demo", password=None)
        )
        self.assertNotIn("Password=old", text)
        self.assertNotIn("Password=", text)


class PresetTests(unittest.TestCase):
    def test_render_preset(self):
        text = mp.render_preset("bridge-token", "m", "61603648")
        self.assertIn("AccountToken=bridge-token", text)
        self.assertIn("AccountLoginOverride=61603648", text)
        self.assertIn("SymbolSuffix=m", text)
        self.assertIn("BackendURL=http://127.0.0.1:8000", text)

    def test_write_preset_is_utf16(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "EA_Bridge.set"
            mp.write_preset(path, "token", "0", "61603648")
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"\xff\xfe"))
            self.assertIn("AccountToken=token", raw.decode("utf-16"))


class LogTests(unittest.TestCase):
    def test_authorized(self):
        line = "2026.11.23 10:00:00   '61603648': authorized on Pepperstone-Demo"
        self.assertTrue(mp.authorized(line, "61603648", "Pepperstone-Demo"))
        self.assertFalse(mp.authorized(line, "61603648", "Other-Demo"))

    def test_redact(self):
        self.assertNotIn("supersecret", mp.redact("token=supersecret&x=1"))
        self.assertNotIn("a" * 40, mp.redact("value " + "a" * 40))


if __name__ == "__main__":
    unittest.main()

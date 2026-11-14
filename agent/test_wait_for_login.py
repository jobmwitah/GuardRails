"""Tests for the MT5 journal login classifier.

Run from the agent directory:

    python -m unittest test_wait_for_login -v
"""

import unittest

from mt5_agent import classify_login_journal

LOGIN = "61603648"


class LoginJournalTests(unittest.TestCase):
    def test_transient_no_connection_before_authorized_is_success(self):
        text = (
            "15:40:50.177 Network '61603648': no connection to Pepperstone-Demo\n"
            "15:40:50.817 Network '61603648': authorized on Pepperstone-Demo "
            "through Access Point 2\n"
            "15:40:51.769 trading has been enabled, demo account - hedging mode\n"
        )
        state, _ = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "authorized")

    def test_reconnect_after_authorized_stays_authorized(self):
        text = (
            "15:40:50.177 Network '61603648': no connection to Pepperstone-Demo\n"
            "15:40:50.817 Network '61603648': authorized on Pepperstone-Demo "
            "through Access Point 2\n"
            "15:41:09.663 connection to Pepperstone-Demo lost\n"
            "15:41:10.413 Network '61603648': authorized on Pepperstone-Demo "
            "through LD1 (ping: 106.59 ms, build 6090)\n"
        )
        state, detail = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "authorized")
        self.assertEqual(detail, "authorized")

    def test_authorized_then_connection_lost_stays_authorized(self):
        text = (
            "15:40:50.817 Network '61603648': authorized on Pepperstone-Demo\n"
            "15:41:09.663 connection to Pepperstone-Demo lost\n"
        )
        state, _ = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "authorized")

    def test_invalid_account_is_rejected(self):
        text = "15:40:50.817 Network '61603648': invalid account\n"
        state, detail = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "rejected")
        self.assertIn("rejected", detail)

    def test_invalid_password_is_rejected(self):
        text = "15:40:50.817 Network '61603648': invalid password\n"
        state, _ = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "rejected")

    def test_authorization_failed_is_rejected(self):
        text = "15:40:50.817 Network '61603648': authorization failed\n"
        state, _ = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "rejected")

    def test_authorization_on_failed_is_rejected(self):
        text = "15:40:50.817 Network '61603648': authorization on Pepperstone-Demo failed\n"
        state, _ = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "rejected")

    def test_investor_mode_is_rejected(self):
        text = (
            "15:40:50.817 Network '61603648': authorized on Pepperstone-Demo\n"
            "15:40:51.769 trading has been enabled, demo account - investor mode\n"
        )
        state, detail = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "rejected")
        self.assertIn("investor", detail)

    def test_no_connection_without_success_waits(self):
        text = "15:40:50.177 Network '61603648': no connection to Pepperstone-Demo\n"
        state, detail = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "no_connection")
        self.assertIn("trade server", detail)

    def test_rejection_then_successful_retry_is_authorized(self):
        text = (
            "15:40:50.817 Network '61603648': invalid password\n"
            "15:40:55.817 Network '61603648': authorized on Pepperstone-Demo\n"
        )
        state, _ = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "authorized")

    def test_success_then_rejection_is_rejected(self):
        text = (
            "15:40:50.817 Network '61603648': authorized on Pepperstone-Demo\n"
            "15:40:55.817 Network '61603648': invalid password\n"
        )
        state, _ = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "rejected")

    def test_other_login_is_ignored(self):
        text = (
            "15:40:50.177 Network '1515282832': no connection to FTMO-Demo\n"
            "15:40:50.817 Network '1515282832': authorized on FTMO-Demo\n"
        )
        state, _ = classify_login_journal(text, LOGIN)
        self.assertEqual(state, "pending")


if __name__ == "__main__":
    unittest.main()

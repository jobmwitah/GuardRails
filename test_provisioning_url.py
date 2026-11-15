"""Tests for the provisioning URLs (public links vs. agent backend)."""

import os
import unittest
from unittest import mock

os.environ.setdefault("DB_PATH", ":memory:")

import provisioning


class AgentBackendUrlTests(unittest.TestCase):
    def test_prefers_dedicated_agent_url(self):
        with mock.patch.object(provisioning, "PUBLIC_BASE_URL", "https://public.example"), \
                mock.patch.object(provisioning, "AGENT_BACKEND_URL", "http://127.0.0.1:8000/"):
            self.assertEqual(provisioning.agent_backend_url(), "http://127.0.0.1:8000")

    def test_falls_back_to_public_url(self):
        with mock.patch.object(provisioning, "PUBLIC_BASE_URL", "https://public.example/"), \
                mock.patch.object(provisioning, "AGENT_BACKEND_URL", ""):
            self.assertEqual(provisioning.agent_backend_url(), "https://public.example")

    def test_fails_closed_when_neither_is_set(self):
        with mock.patch.object(provisioning, "PUBLIC_BASE_URL", ""), \
                mock.patch.object(provisioning, "AGENT_BACKEND_URL", ""):
            with self.assertRaises(provisioning.ProvisioningError):
                provisioning.agent_backend_url()

    def test_ride_along_values_are_trimmed(self):
        with mock.patch.object(provisioning, "PUBLIC_BASE_URL", "https://public.example"), \
                mock.patch.object(provisioning, "AGENT_BACKEND_URL", "  http://127.0.0.1:8000  "):
            self.assertEqual(provisioning.agent_backend_url(), "http://127.0.0.1:8000")


if __name__ == "__main__":
    unittest.main()

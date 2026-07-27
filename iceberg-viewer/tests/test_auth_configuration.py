#!/usr/bin/env python3
"""Fail-closed startup checks for authentication configuration."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / 'mock-backend-py' / 'server.py'


def run_startup(**overrides):
    excluded = {
        'MOCK_PG_DSN',
        'MOCK_RECORD',
        'MOCK_REQUIRE_AUTH',
        'MOCK_ROBOT_TOKEN',
        'MOCK_YT_UPSTREAM',
    }
    environment = {
        key: value for key, value in os.environ.items()
        if key not in excluded
    }
    environment.update(overrides)
    return subprocess.run(
        [sys.executable, str(SERVER), '0'],
        env=environment, capture_output=True, text=True, timeout=10,
        check=False)


class TestAuthenticationConfiguration(unittest.TestCase):
    def test_delegated_verification_requires_strict_authentication(self):
        process = run_startup(
            MOCK_YT_UPSTREAM='https://proxy.yt.internal')

        self.assertNotEqual(process.returncode, 0)
        self.assertIn(
            'MOCK_YT_UPSTREAM requires MOCK_REQUIRE_AUTH=1',
            process.stderr)

    def test_published_robot_token_is_rejected_by_the_backend(self):
        process = run_startup(
            MOCK_REQUIRE_AUTH='1',
            MOCK_ROBOT_TOKEN='mock-robot-token')

        self.assertNotEqual(process.returncode, 0)
        self.assertIn(
            'must be changed from the published mock-robot-token',
            process.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)

"""Narrow TEST-only prompt selector coverage for HostRuntime's Compose policy.

Uses fake Docker data only; never touches Docker, credentials, or running services.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from host_runtime import HostRuntime  # noqa: E402
from host_runtime_config import HostRuntimeError  # noqa: E402
from test_host_runtime import FakeDocker  # noqa: E402


class PromptVersionComposePolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-v7-policy-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeDocker()
        self.runtime = HostRuntime({"tossApp": self.root}, runner=self.fake.run)

    def check(self, mutate=None, mode="toss-demo", allowed=True):
        base = self.fake.config

        def configured(requested_mode):
            model = base(requested_mode)
            if mutate:
                mutate(model)
            return model

        self.fake.config = configured
        try:
            if allowed:
                self.assertRegex(self.runtime.compose_config(self.root, mode), r"^[a-f0-9]{64}$")
            else:
                with self.assertRaises(HostRuntimeError) as caught:
                    self.runtime.compose_config(self.root, mode)
                self.assertEqual(caught.exception.code, "WRONG_COMPOSE_CONFIG")
        finally:
            self.fake.config = base

    @staticmethod
    def selector(model, value):
        model["services"]["api"]["environment"]["READING_TEST_PROMPT_VERSION"] = value

    def test_absent_selector_preserves_original_contract(self):
        self.check()

    def test_exact_v1_and_v7_accepted_with_distinct_digest(self):
        digests = []
        for version in ("tarot-grounded.v1", "tarot-grounded.v7"):
            base = self.fake.config
            self.fake.config = lambda m, base=base, version=version: self._with_version(base(m), version)
            try:
                digests.append(self.runtime.compose_config(self.root, "toss-demo"))
            finally:
                self.fake.config = base
        self.assertNotEqual(*digests)

    def _with_version(self, model, version):
        self.selector(model, version)
        return model

    def test_invalid_versions_fail_closed(self):
        for value in ("", "tarot-grounded.v6", "tarot-grounded.v7 ",
                      "TAROT-GROUNDED.V7", None, 7, [], {}):
            with self.subTest(value=value):
                self.check(lambda x, value=value: self.selector(x, value), allowed=False)

    def test_non_test_app_env_rejected(self):
        for env in ("production", "development"):
            with self.subTest(env=env):
                def invalid(model, env=env):
                    self.selector(model, "tarot-grounded.v7")
                    model["services"]["api"]["environment"]["APP_ENV"] = env
                self.check(invalid, allowed=False)

    def test_worker_selector_is_not_allowed(self):
        self.check(lambda x: x["services"]["worker"]["environment"].update(
            {"READING_TEST_PROMPT_VERSION": "tarot-grounded.v7"}), allowed=False)

    def test_unrelated_api_key_rejected(self):
        self.check(lambda x: x["services"]["api"]["environment"].update(
            {"UNREVIEWED_OPTION": "true"}), allowed=False)

    def test_missing_legacy_required_key_rejected(self):
        self.check(lambda x: x["services"]["api"]["environment"].pop(
            "AUTH_MODE"), allowed=False)

    def test_public_api_port_rejected(self):
        self.check(lambda x: x["services"]["api"]["ports"][0].update(
            {"host_ip": "0.0.0.0"}), allowed=False)

    def test_privileged_container_rejected(self):
        self.check(lambda x: x["services"]["api"].update(
            {"privileged": True}), allowed=False)

    def test_unexpected_secret_rejected(self):
        self.check(lambda x: x["services"]["api"]["secrets"].append(
            {"source": "unexpected"}), allowed=False)

    def test_mismatched_demo_mode_rejected(self):
        def mismatch(model):
            self.selector(model, "tarot-grounded.v7")
            model["services"]["api"]["environment"]["READING_MONETIZATION_TEST_MODE"] = "disabled"
        self.check(mismatch, mode="toss-demo", allowed=False)


if __name__ == "__main__":
    unittest.main()

import unittest

from runtime_rollout_worker import _safe_failure_code


class CodeError(RuntimeError):
    code = "ROLLOUT_SOURCE_CHANGED"


class RuntimeRolloutWorkerTest(unittest.TestCase):
    def test_safe_failure_code_preserves_reviewed_runtime_codes(self):
        self.assertEqual(
            _safe_failure_code(RuntimeError("RUNTIME_TESTS_FAILED")),
            "RUNTIME_TESTS_FAILED",
        )
        self.assertEqual(
            _safe_failure_code(CodeError("details must stay hidden")),
            "ROLLOUT_SOURCE_CHANGED",
        )

    def test_safe_failure_code_redacts_unstructured_errors(self):
        self.assertEqual(
            _safe_failure_code(RuntimeError("private path /tmp/example")),
            "ROLLOUT_FAILED",
        )


if __name__ == "__main__":
    unittest.main()

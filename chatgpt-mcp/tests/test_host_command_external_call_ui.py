from pathlib import Path
import unittest


class ExternalCallApprovalUiTests(unittest.TestCase):
    def test_external_call_is_explicitly_labeled_as_cost_bearing_api_work(self):
        html = (Path(__file__).resolve().parents[1] / "host-command-approval.html").read_text()
        self.assertIn('"external_call"', html)
        self.assertIn("외부 API 호출", html)
        self.assertIn("비용", html)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from agent_events import sanitize_agent_event


class AgentEventTest(unittest.TestCase):
    def test_public_event_copies_only_bounded_metadata(self):
        event = sanitize_agent_event({
            "phase": "WORKING", "code": "FILE_CHANGE",
            "paths": ["src/demo.py", "../secret", ".env"],
            "usage": {"input_tokens": 4, "bad": 9},
            "usage_scope": "turn", "model": "gpt-5.6",
            "model_source": "event", "stdout": "private",
        })
        self.assertEqual(event, {
            "phase": "WORKING", "code": "FILE_CHANGE",
            "paths": ["src/demo.py"],
            "usage": {"input_tokens": 4}, "usage_scope": "turn",
            "model": "gpt-5.6", "model_source": "event",
        })

    def test_public_event_rejects_unknown_phase_or_code(self):
        self.assertIsNone(sanitize_agent_event({"phase": "THINKING", "code": "RAW"}))


if __name__ == "__main__":
    unittest.main()

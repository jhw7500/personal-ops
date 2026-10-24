from __future__ import annotations

import unittest

from agent_tools import AgentTools


class ClosingManager:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1


class AgentShutdownTest(unittest.IsolatedAsyncioTestCase):
    async def test_tools_close_only_the_manager_instance_they_own(self):
        first = ClosingManager()
        second = ClosingManager()
        tools = AgentTools({}, "/tmp/unused-agent-state", manager=first)
        other = AgentTools({}, "/tmp/unused-agent-state", manager=second)
        await tools.close()
        self.assertEqual(first.closed, 1)
        self.assertEqual(second.closed, 0)
        await other.close()
        self.assertEqual(second.closed, 1)

    async def test_tools_without_materialized_manager_do_not_create_one_on_shutdown(self):
        tools = AgentTools({}, "/tmp/unused-agent-state")
        await tools.close()
        self.assertIsNone(tools._manager)


if __name__ == "__main__":
    unittest.main()

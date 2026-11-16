"""Policy modes of the confirming tools: python -m unittest discover tests"""

import asyncio
import os
import unittest
from types import SimpleNamespace

from paperclip_mcp import server

POLICY = os.path.join(os.path.dirname(__file__), "..", "policy.yaml")
NO_ELICITATION = SimpleNamespace(client_capabilities=None)


class Policy(unittest.TestCase):
    def setUp(self):
        self.posts = []

        async def call(method, path, **kw):
            if method == "POST":
                self.posts.append((path, kw.get("json")))
                return {}
            if path.startswith("/approvals/"):
                return {"type": "hire_agent", "payload": {"name": "Max"}}
            return [{"id": "a1", "name": "Quinn", "status": "paused"}]

        async def company_ids(company):
            return [("c1", "DM-Consulting")]

        self.saved = server._call, server._company_ids, dict(server.ACTIVE)
        server._call, server._company_ids = call, company_ids

    def tearDown(self):
        server._call, server._company_ids = self.saved[:2]
        server.ACTIVE.clear()
        server.ACTIVE.update(self.saved[2])

    def test_repo_policy(self):
        tools = server.register(server.load_policy(POLICY))
        self.assertIn("interaction_respond=allow", tools)
        self.assertIn("agent_resume=allow", tools)
        self.assertIn("approval_decision=ask", tools)
        self.assertIn("routine_run=ask", tools)
        self.assertNotIn("plan_confirm=allow", tools)

    def test_other_decision_tools_refuse_allow(self):
        for name in ("approval_decision", "plan_confirm", "action_request_decision", "routine_run", "issue_update"):
            with self.subTest(name), self.assertRaises(SystemExit):
                server.register({name: "allow"})

    def test_respond_allow_without_elicitation(self):
        server.ACTIVE["interaction_respond"] = "allow"
        out = asyncio.run(server.interaction_respond("DMC-1", "x1", '[{"questionId": "q", "optionIds": ["o"]}]', NO_ELICITATION))
        self.assertEqual(out, "Answer sent on DMC-1.")
        self.assertEqual(self.posts, [("/issues/DMC-1/interactions/x1/respond", {"answers": [{"questionId": "q", "optionIds": ["o"]}]})])

    def test_respond_ask_without_elicitation_refused(self):
        server.ACTIVE["interaction_respond"] = "ask"
        out = asyncio.run(server.interaction_respond("DMC-1", "x1", "[]", NO_ELICITATION))
        self.assertTrue(out.startswith("Refused"))
        self.assertEqual(self.posts, [])

    def test_resume_allow_without_elicitation(self):
        server.ACTIVE["agent_resume"] = "allow"
        out = asyncio.run(server.agent_resume("DMC", "Quinn", NO_ELICITATION))
        self.assertEqual(out, "Quinn resumed.")
        self.assertEqual(self.posts, [("/agents/a1/resume", None)])

    def test_approval_still_asks(self):
        server.ACTIVE["approval_decision"] = "ask"
        out = asyncio.run(server.approval_decision("ap1", "approve", "", NO_ELICITATION))
        self.assertTrue(out.startswith("Refused"))
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()

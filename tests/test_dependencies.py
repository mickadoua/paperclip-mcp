"""`dependencies` against a mocked Paperclip API: python -m unittest discover tests"""

import asyncio
import copy
import unittest

from paperclip_mcp import server

PROJECTS = [{"id": "p1", "name": "Templates (template-site, template-android)"}, {"id": "p2", "name": "Voxbulle"}]
AGENTS = [{"id": "a1", "name": "Sacha"}]
ISSUES = [
    {"id": "i1", "identifier": "DMC-482", "status": "blocked", "assigneeAgentId": "a1", "projectId": "p1",
     "blockedBy": [{"id": "i9", "identifier": "DMC-474", "status": "done", "title": "Modèles légaux"}],
     "blockedTransitionAt": "2026-11-09T10:00:00Z", "title": "template-site : recopier les modèles légaux"},
    {"id": "i2", "identifier": "DMC-110", "status": "blocked", "assigneeAgentId": "a1", "projectId": None,
     "blockedBy": [], "blockedTransitionAt": "2026-11-02T10:00:00Z", "title": "Brancher la Search Console",
     "unblockDescriptor": {"owner": "board", "action": "Créer la clé"}},
]
CLOSED = {"i9": {"id": "i9", "projectId": "p2"}}


class Dependencies(unittest.TestCase):
    def setUp(self):
        self.calls = []

        async def call(method, path, **kw):
            self.calls.append(path)
            if path.endswith("/projects"):
                return PROJECTS
            if path.endswith("/agents"):
                return AGENTS
            if path.endswith("/issues"):
                return copy.deepcopy(ISSUES)
            ref = path.rsplit("/", 1)[-1]
            if ref not in CLOSED:
                raise server.ToolError(f"Paperclip GET {path} -> 404 : not found")
            return CLOSED[ref]

        async def company_ids(company):
            return [("c1", "DM-Consulting")]

        self.saved = server._call, server._company_ids
        server._call, server._company_ids = call, company_ids

    def tearDown(self):
        server._call, server._company_ids = self.saved

    def test_project_after_since(self):
        lines = asyncio.run(server.dependencies()).splitlines()
        self.assertEqual(lines[0], "## DM-Consulting (3)")
        self.assertEqual(
            lines[1],
            "DMC-482 [blocked] by=Sacha after=DMC-474 waits=- since=2026-11-09"
            " project=Templates_(template-site,_template-android) · template-site : recopier les modèles légaux",
        )
        self.assertEqual(lines[2], "DMC-474 [done] by=- after=- waits=- since=- project=Voxbulle · Modèles légaux")
        self.assertEqual(lines[3], "DMC-110 [blocked] by=Sacha after=- waits=board since=2026-11-02 project=- · Brancher la Search Console")
        self.assertEqual(lines[4], "  awaited: Créer la clé")

    def test_closed_blocker_read_once(self):
        asyncio.run(server.dependencies())
        self.assertEqual([c for c in self.calls if c.startswith("/issues/")], ["/issues/i9"])

    def test_unreadable_blocker_has_no_project(self):
        CLOSED.pop("i9")
        try:
            lines = asyncio.run(server.dependencies()).splitlines()
        finally:
            CLOSED["i9"] = {"id": "i9", "projectId": "p2"}
        self.assertEqual(lines[2], "DMC-474 [done] by=- after=- waits=- since=- project=- · Modèles légaux")


if __name__ == "__main__":
    unittest.main()

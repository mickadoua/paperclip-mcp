"""`attention` against a mocked Paperclip API: python -m unittest discover tests"""

import asyncio
import unittest

from paperclip_mcp import server

CARDS = [
    {"sourceKind": "issue_review", "subject": {"identifier": f"DMC-{n}", "title": f"Carte {n}"}, "whyNow": "revue"}
    for n in range(1, 121)
]


class Refused(Exception):
    pass


class Attention(unittest.TestCase):
    def setUp(self):
        self.calls = []

        # Same rules as GET /companies/{id}/attention on the NAS server (2026.1005.0):
        # 50 cards per page by default, the whole feed with `all`, which refuses `limit` and `cursor`.
        async def call(method, path, params=None, **kw):
            params = dict(params or {})
            self.calls.append(params)
            if params.get("all") == "true":
                if "limit" in params or "cursor" in params:
                    raise Refused("400 all cannot be combined with cursor or limit")
                return {"items": CARDS, "nextCursor": None}
            return {"items": CARDS[:50], "nextCursor": "50"}

        async def company_ids(company):
            return [("c1", "DM-Consulting")]

        self.saved = server._call, server._company_ids
        server._call, server._company_ids = call, company_ids

    def tearDown(self):
        server._call, server._company_ids = self.saved

    def test_reads_every_card(self):
        lines = asyncio.run(server.attention()).splitlines()
        self.assertEqual(lines[0], "## DM-Consulting (120)")
        self.assertEqual(len(lines), 121)
        self.assertEqual(lines[1], "- [issue_review] DMC-1 Carte 1 — revue")
        self.assertEqual(lines[-1], "- [issue_review] DMC-120 Carte 120 — revue")

    def test_one_call_with_all_only(self):
        asyncio.run(server.attention())
        self.assertEqual(self.calls, [{"all": "true"}])


if __name__ == "__main__":
    unittest.main()

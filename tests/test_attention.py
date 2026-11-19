"""`attention` against a mocked Paperclip API: python -m unittest discover tests"""

import asyncio
import unittest

from paperclip_mcp import server

CARDS = [
    {"sourceKind": "issue_review", "subject": {"identifier": f"DMC-{n}", "title": f"Carte {n}"}, "whyNow": "revue"}
    for n in range(1, 121)
]


class Attention(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.page = 50

        async def call(method, path, params=None, **kw):
            self.calls.append(dict(params or {}))
            start = int((params or {}).get("cursor") or 0)
            end = len(CARDS) if self.page is None else start + self.page
            return {"items": CARDS[start:end], "nextCursor": str(end) if end < len(CARDS) else None}

        async def company_ids(company):
            return [("c1", "DM-Consulting")]

        self.saved = server._call, server._company_ids
        server._call, server._company_ids = call, company_ids

    def tearDown(self):
        server._call, server._company_ids = self.saved

    def test_reads_every_page(self):
        lines = asyncio.run(server.attention()).splitlines()
        self.assertEqual(lines[0], "## DM-Consulting (120)")
        self.assertEqual(len(lines), 121)
        self.assertEqual(lines[1], "- [issue_review] DMC-1 Carte 1 — revue")
        self.assertEqual(lines[-1], "- [issue_review] DMC-120 Carte 120 — revue")
        self.assertEqual([c.get("cursor") for c in self.calls], [None, "50", "100"])
        self.assertTrue(all(c["all"] == "true" for c in self.calls))

    def test_all_in_one_response(self):
        self.page = None
        lines = asyncio.run(server.attention()).splitlines()
        self.assertEqual(lines[0], "## DM-Consulting (120)")
        self.assertEqual(len(self.calls), 1)

    def test_repeated_cursor_stops(self):
        async def call(method, path, params=None, **kw):
            self.calls.append(dict(params or {}))
            return {"items": CARDS[:50], "nextCursor": "same"}

        server._call = call
        lines = asyncio.run(server.attention()).splitlines()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(lines[0], "## DM-Consulting (100)")


if __name__ == "__main__":
    unittest.main()

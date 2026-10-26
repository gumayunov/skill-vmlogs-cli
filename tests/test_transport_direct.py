import json
import unittest

from tests.fakes import FakeServer, victorialogs
from vmlogs_cli.errors import CliError
from vmlogs_cli.transport import DirectTransport


class DirectTransportTest(unittest.TestCase):
    def setUp(self):
        self.vl = FakeServer(victorialogs())
        self.transport = DirectTransport(self.vl.url + "/")

    def tearDown(self):
        self.vl.close()

    def test_query_streams_lines(self):
        params = {"query": "*", "limit": "5", "start": "2026-10-26T00:00:00Z"}
        lines = list(self.transport.fetch("query", params))
        self.assertEqual([json.loads(line)["_msg"] for line in lines], ["second", "first"])
        method, path, sent, _ = self.vl.requests[-1]
        self.assertEqual((method, path), ("POST", "/select/logsql/query"))
        self.assertEqual(sent, params)

    def test_bad_query_shows_victorialogs_text(self):
        with self.assertRaisesRegex(
            CliError, "VictoriaLogs не принял запрос: cannot parse .*context"
        ):
            self.transport.fetch("query", {"query": "x | bad("})

    def test_unreachable(self):
        transport = DirectTransport("http://127.0.0.1:9", timeout=2)
        with self.assertRaisesRegex(CliError, "не ответил"):
            transport.fetch("query", {"query": "*"})

    def test_describe(self):
        self.assertEqual(self.transport.describe(), f"direct {self.vl.url}")


if __name__ == "__main__":
    unittest.main()

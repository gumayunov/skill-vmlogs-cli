import http.client
import json
import unittest
from unittest import mock

from tests.fakes import FakeServer, victorialogs
from vmlogs_cli.errors import CliError
from vmlogs_cli.transport import DirectTransport, iter_lines


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

    def test_describe_hides_userinfo(self):
        text = DirectTransport("http://u:p@vl.example.com:9428").describe()
        self.assertEqual(text, "direct http://vl.example.com:9428")

    def test_http_exception_on_open(self):
        with mock.patch(
            "urllib.request.OpenerDirector.open", side_effect=http.client.BadStatusLine("x")
        ):
            with self.assertRaisesRegex(CliError, "не ответил"):
                self.transport.fetch("query", {"query": "*"})


class BrokenResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield b"one\n"
        raise http.client.IncompleteRead(b"")


class IterLinesTest(unittest.TestCase):
    def test_incomplete_read_is_cli_error(self):
        lines = iter_lines(BrokenResponse())
        self.assertEqual(next(lines), "one")
        with self.assertRaisesRegex(CliError, "ответ оборвался"):
            next(lines)


if __name__ == "__main__":
    unittest.main()

import stat
import tempfile
import unittest
from pathlib import Path

from tests.fakes import GOOD, FakeServer, grafana, no_proxy_env
from vmlogs_cli.errors import CliError
from vmlogs_cli.login import GrafanaSession, cookie_file, merge_cookies, save_secret
from vmlogs_cli.transport import GrafanaTransport


class CookieHelpersTest(unittest.TestCase):
    def test_merge_updates_and_appends(self):
        self.assertEqual(
            merge_cookies(
                "a=1; grafana_session=old", ["grafana_session=new; Path=/", "b=2; HttpOnly"]
            ),
            "a=1; grafana_session=new; b=2",
        )

    def test_merge_empty_value_drops_cookie(self):
        self.assertEqual(merge_cookies("a=1; b=2", ["b=; Max-Age=0"]), "a=1")

    def test_cookie_file_per_host(self):
        self.assertEqual(
            cookie_file(Path("/c"), "https://grafana.example.com:8443/"),
            Path("/c/cookies/grafana.example.com:8443"),
        )

    def test_cookie_file_without_userinfo(self):
        self.assertEqual(
            cookie_file(Path("/c"), "https://u:p@grafana.example.com"),
            Path("/c/cookies/grafana.example.com"),
        )

    def test_save_secret_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cookies" / "host"
            path.parent.mkdir()
            path.write_text("old")
            path.chmod(0o644)
            save_secret(path, "a=1")
            self.assertEqual(path.read_text(), "a=1\n")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class GrafanaTransportTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer(grafana())
        self.tmp = tempfile.TemporaryDirectory()
        self.cookie = Path(self.tmp.name) / "cookie"
        self.session = GrafanaSession(self.server.url, self.cookie)

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def transport(self, ds="Logs"):
        return GrafanaTransport(self.server.url, ds, self.session)

    def test_query_through_datasource_proxy(self):
        save_secret(self.cookie, GOOD)
        lines = list(self.transport().fetch("query", {"query": "*"}))
        self.assertEqual(len(lines), 2)
        method, path, params, headers = self.server.requests[-1]
        self.assertEqual(path, "/api/datasources/proxy/uid/logs-uid/select/logsql/query")
        self.assertEqual((method, params["query"]), ("POST", "*"))
        self.assertEqual(headers["x-grafana-org-id"], "1")

    def test_datasource_by_uid(self):
        save_secret(self.cookie, GOOD)
        self.assertEqual(len(list(self.transport("logs-uid").fetch("query", {"query": "*"}))), 2)

    def test_datasources_lists_only_logs(self):
        save_secret(self.cookie, GOOD)
        self.assertEqual(self.transport().datasources(), [("logs-uid", "Logs")])

    def test_unknown_datasource(self):
        save_secret(self.cookie, GOOD)
        with self.assertRaisesRegex(CliError, "«Nope» не найден. Есть: Logs"):
            self.transport("Nope").fetch("query", {"query": "*"})

    def test_missing_datasource(self):
        save_secret(self.cookie, GOOD)
        with self.assertRaisesRegex(CliError, "не задан датасорс"):
            self.transport("").fetch("query", {"query": "*"})

    def test_401_rotates_saves_and_retries(self):
        save_secret(self.cookie, "grafana_session=old")
        self.assertEqual(len(list(self.transport().fetch("query", {"query": "*"}))), 2)
        self.assertEqual(
            self.cookie.read_text().strip(), "grafana_session=good; grafana_session_expiry=99"
        )

    def test_401_without_rotation_asks_login(self):
        save_secret(self.cookie, "grafana_session=dead")
        with self.assertRaisesRegex(CliError, "vmlogs-cli login"):
            self.transport().fetch("query", {"query": "*"})

    def test_no_cookie_asks_login(self):
        with self.assertRaisesRegex(CliError, "нет входа в .*vmlogs-cli login"):
            self.transport().fetch("query", {"query": "*"})

    def test_bad_query(self):
        save_secret(self.cookie, GOOD)
        with self.assertRaisesRegex(CliError, "VictoriaLogs не принял запрос: cannot parse"):
            self.transport().fetch("query", {"query": "x | bad("})

    def test_unreachable_hides_userinfo(self):
        base = "http://u:p@127.0.0.1:9"
        session = GrafanaSession(base, self.cookie, timeout=2)
        save_secret(self.cookie, GOOD)
        transport = GrafanaTransport(base, "Logs", session, timeout=2)
        with no_proxy_env(), self.assertRaises(CliError) as ctx:
            transport.datasources()
        self.assertIn("http://127.0.0.1:9 не ответил", str(ctx.exception))
        self.assertNotIn("u:p", str(ctx.exception))

    def test_no_cookie_message_hides_userinfo(self):
        session = GrafanaSession("http://u:p@127.0.0.1:9", self.cookie)
        with self.assertRaises(CliError) as ctx:
            session.header()
        self.assertNotIn("u:p", str(ctx.exception))

    def test_redirect_reports_location(self):
        server = FakeServer(grafana(redirect=True))
        try:
            save_secret(self.cookie, GOOD)
            transport = GrafanaTransport(
                server.url, "Logs", GrafanaSession(server.url, self.cookie)
            )
            with self.assertRaisesRegex(CliError, "отвечает 302 на https://sso.example.com/login"):
                transport.fetch("query", {"query": "*"})
        finally:
            server.close()


class GrafanaSessionTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer(grafana())
        self.tmp = tempfile.TemporaryDirectory()
        self.cookie = Path(self.tmp.name) / "cookie"
        self.session = GrafanaSession(self.server.url, self.cookie)

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def test_refresh_keeps_valid_cookie(self):
        save_secret(self.cookie, GOOD)
        self.assertTrue(self.session.refresh())
        self.assertEqual(self.cookie.read_text().strip(), GOOD)

    def test_refresh_without_file(self):
        self.assertFalse(self.session.refresh())

    def test_valid(self):
        self.assertTrue(self.session.valid(GOOD))
        self.assertFalse(self.session.valid("grafana_session=dead"))
        self.assertFalse(self.session.valid(""))


if __name__ == "__main__":
    unittest.main()

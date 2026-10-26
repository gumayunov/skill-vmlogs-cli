import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.fakes import GOOD, FakeServer, grafana, victorialogs
from vmlogs_cli import __version__
from vmlogs_cli.cli import main, make_transport, run
from vmlogs_cli.config import config_dir, load_settings
from vmlogs_cli.errors import CliError
from vmlogs_cli.login import cookie_file, save_secret
from vmlogs_cli.transport import DirectTransport, GrafanaTransport, KubectlTransport

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent


class CliTest(unittest.TestCase):
    def setUp(self):
        self.vl = FakeServer(victorialogs())
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.profile = self.dir / "env"
        self.profile.write_text(f"VMLOGS_TRANSPORT=direct\nVMLOGS_URL={self.vl.url}\n")
        self.env = {"HOME": str(self.dir), "VMLOGS_ENV": str(self.profile)}

    def tearDown(self):
        self.vl.close()
        self.tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        code = run(list(argv), self.env, str(self.dir), NOW, out, err)
        return code, out.getvalue(), err.getvalue()

    def last_params(self):
        return self.vl.requests[-1][2]

    def test_query_short_ascending(self):
        code, out, err = self.cli("query", "*", "--since", "2h")
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            ["2026-10-26T07:03:12Z api ERROR first", "2026-10-26T07:03:14Z api INFO second"],
        )
        self.assertEqual(err, "")
        params = self.last_params()
        self.assertEqual((params["start"], params["limit"]), ("2026-10-26T10:00:00Z", "50"))
        self.assertNotIn("end", params)

    def test_start_end(self):
        self.cli("query", "*", "--start", "2026-10-26T07:00:00Z", "--end", "1h")
        params = self.last_params()
        self.assertEqual(
            (params["start"], params["end"]), ("2026-10-26T07:00:00Z", "2026-10-26T11:00:00Z")
        )

    def test_limit_hint(self):
        _, _, err = self.cli("query", "*", "--limit", "2")
        self.assertIn("показаны последние 2 строк", err)

    def test_zero_rows_hint(self):
        code, out, err = self.cli("query", "nothing")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("0 строк", err)
        self.assertIn("bulk*", err)

    def test_json_keeps_victorialogs_order(self):
        _, out, _ = self.cli("query", "*", "-o", "json")
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([row["_msg"] for row in rows], ["second", "first"])
        self.assertNotIn("_stream_id", rows[0])

    def test_all_fields(self):
        _, out, _ = self.cli("query", "*", "-o", "json", "--all-fields")
        self.assertIn("_stream_id", json.loads(out.splitlines()[0]))

    def test_stats_rows(self):
        _, out, _ = self.cli("query", "* | stats count() c")
        self.assertEqual(out, "c=2\n")

    def test_width_marker(self):
        _, out, _ = self.cli("query", "*", "--width", "25")
        self.assertTrue(out.splitlines()[0].endswith("get 0a1b/2026-10-26T07:03:12Z]"))

    def test_out_file(self):
        target = self.dir / "rows.jsonl"
        code, out, _ = self.cli("query", "*", "--out", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(len(target.read_text().splitlines()), 2)
        self.assertIn(f"2 строк → {target}", out)
        self.assertIn("поля: _msg, _time, kubernetes.container_name, level", out)

    def test_out_file_is_atomic(self):
        target = self.dir / "rows.jsonl"
        target.write_text("old\n")

        class Broken:
            def describe(self):
                return "broken"

            def fetch(self, endpoint, params):
                yield json.dumps({"_msg": "first"})
                raise CliError("обрыв посреди потока")

        with mock.patch("vmlogs_cli.cli.make_transport", return_value=Broken()):
            code, _, err = self.cli("query", "*", "--out", str(target))
        self.assertEqual(code, 1)
        self.assertIn("обрыв посреди потока", err)
        self.assertEqual(target.read_text(), "old\n")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["env", "rows.jsonl"])

    def test_out_limit_hint(self):
        target = self.dir / "rows.jsonl"
        _, _, err = self.cli("query", "*", "--limit", "2", "--out", str(target))
        self.assertIn("записаны последние 2 строк окна — есть ещё", err)
        self.assertNotIn("показаны последние", err)

    def test_stats_limit_hint(self):
        _, _, err = self.cli("query", "* | stats count() c", "--limit", "1")
        self.assertIn("показано 1 строк результата — возможно, есть ещё", err)
        self.assertIn("| sort … | limit", err)

    def test_fields_empty_hint(self):
        code, out, err = self.cli("fields", "nothing")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("0 полей за окно с 2026-10-26T11:00:00Z — проверь фильтр и окно", err)

    def test_values_empty_hint(self):
        code, out, err = self.cli("values", "nothing", "level")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("0 значений за окно с 2026-10-26T11:00:00Z — проверь фильтр и окно", err)

    def test_invalid_numeric_flags(self):
        cases = [
            (("query", "*", "--limit", "0"), "--limit: должно быть целое не меньше 1"),
            (("values", "*", "f", "--limit", "-3"), "--limit: должно быть целое не меньше 1"),
            (("query", "*", "--width", "-1"), "--width: должно быть целое не меньше 0"),
            (("check", "--timeout", "0"), "--timeout: должно быть целое не меньше 1"),
            (("query", "*", "--limit", "abc"), "--limit: должно быть целое не меньше 1"),
        ]
        for argv, text in cases:
            with self.subTest(argv=argv):
                err = io.StringIO()
                with mock.patch("sys.stderr", err), self.assertRaises(SystemExit) as ctx:
                    run(list(argv), self.env, str(self.dir), NOW, io.StringIO(), io.StringIO())
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn(text, err.getvalue())

    def test_width_zero_is_allowed(self):
        code, _, _ = self.cli("query", "*", "--width", "0")
        self.assertEqual(code, 0)

    def test_out_file_unwritable(self):
        code, _, err = self.cli("query", "*", "--out", str(self.dir / "missing" / "x.jsonl"))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("vmlogs-cli: не удалось записать"), err)
        self.assertNotIn("Traceback", err)

    def test_unexpected_json_shape(self):
        shaped = FakeServer(lambda *args: (200, [], "[1, 2]"))
        self.addCleanup(shaped.close)
        profile = self.dir / "shaped-env"
        profile.write_text(f"VMLOGS_TRANSPORT=direct\nVMLOGS_URL={shaped.url}\n")
        code, _, err = self.cli("fields", "*", "--env", str(profile))
        self.assertEqual(code, 1)
        self.assertIn("ответ не похож на ответ VictoriaLogs", err)
        self.assertNotIn("Traceback", err)

    def test_bad_query(self):
        code, _, err = self.cli("query", "x | bad(")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("vmlogs-cli: VictoriaLogs не принял запрос"), err)

    def test_fields(self):
        _, out, err = self.cli("fields", "*")
        self.assertEqual(out, "2\t_msg\n2\tlevel\n")
        self.assertIn("скрыто служебных полей: 1", err)

    def test_values(self):
        _, out, _ = self.cli("values", "*", "level")
        self.assertEqual(out, "1\terror\n1\tinfo\n")
        self.assertEqual(self.last_params()["field"], "level")

    def test_get(self):
        code, out, _ = self.cli("get", "0a1b/2026-10-26T07:03:12Z")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["_msg"], "first")
        self.assertEqual(
            self.last_params()["query"],
            "_stream_id:0a1b _time:[2026-10-26T07:03:12Z, 2026-10-26T07:03:12Z]",
        )

    def test_get_bad_ref(self):
        code, _, err = self.cli("get", "nonsense")
        self.assertEqual(code, 1)
        self.assertIn("не похожа на <stream_id>/<time>", err)

    def test_get_bad_ref_checked_before_request(self):
        for ref in (
            "0a1b/now",
            "ZZ/2026-10-26T07:03:12Z",
            "0a1b | x/2026-10-26T07:03:12Z",
            "0a1b/ x",
        ):
            with self.subTest(ref=ref):
                before = len(self.vl.requests)
                code, _, err = self.cli("get", ref)
                self.assertEqual(code, 1)
                self.assertIn("не похожа на <stream_id>/<time>", err)
                self.assertEqual(len(self.vl.requests), before)

    def test_check(self):
        code, out, _ = self.cli("check")
        self.assertEqual(code, 0)
        self.assertEqual(
            out.splitlines(),
            [
                f"профиль: {self.profile}",
                f"транспорт: direct {self.vl.url}",
                "записей за 5 минут: 2",
            ],
        )

    def test_login_needs_grafana(self):
        code, _, err = self.cli("login")
        self.assertEqual(code, 1)
        self.assertIn("только для VMLOGS_TRANSPORT=grafana", err)

    def test_no_profile(self):
        del self.env["VMLOGS_ENV"]
        code, _, err = self.cli("check")
        self.assertEqual(code, 1)
        self.assertIn("нет профиля", err)

    def test_options_after_command(self):
        code, _, _ = self.cli(
            "query", "*", "--env", str(self.profile), "--timeout", "5", "--since", "2h"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.last_params()["start"], "2026-10-26T10:00:00Z")


class GrafanaCliTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer(grafana())
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.profile = self.dir / "env"
        self.profile.write_text(
            f"VMLOGS_TRANSPORT=grafana\nVMLOGS_GRAFANA_URL={self.server.url}\nVMLOGS_DATASOURCE=Logs\n"
        )
        self.env = {"HOME": str(self.dir), "VMLOGS_ENV": str(self.profile)}

    def tearDown(self):
        self.server.close()
        self.tmp.cleanup()

    def test_datasources(self):
        save_secret(cookie_file(config_dir(self.env), self.server.url), GOOD)
        out = io.StringIO()
        code = run(["datasources"], self.env, str(self.dir), NOW, out, io.StringIO())
        self.assertEqual((code, out.getvalue()), (0, "logs-uid\tLogs\n"))

    def test_make_transport_grafana(self):
        settings = load_settings(self.env, str(self.dir), None)
        transport = make_transport(settings, None, 7, self.env)
        self.assertIsInstance(transport, GrafanaTransport)
        self.assertEqual((transport.base_url, transport.datasource), (self.server.url, "Logs"))
        self.assertEqual(transport.timeout, 7)
        self.assertEqual(transport.session.path, cookie_file(config_dir(self.env), self.server.url))
        self.assertEqual(make_transport(settings, "Other", 7, self.env).datasource, "Other")


class MakeTransportTest(unittest.TestCase):
    def settings(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "env"
            profile.write_text(text)
            return load_settings({"VMLOGS_ENV": str(profile)}, tmp, None)

    def test_direct(self):
        settings = self.settings("VMLOGS_TRANSPORT=direct\nVMLOGS_URL=http://vl.example.com:9428\n")
        transport = make_transport(settings, None, 9, {})
        self.assertIsInstance(transport, DirectTransport)
        self.assertEqual((transport.url, transport.timeout), ("http://vl.example.com:9428", 9))

    def test_kubectl(self):
        settings = self.settings(
            "VMLOGS_TRANSPORT=kubectl\nVMLOGS_KUBE_SERVICE=monitoring/vl:9428\n"
            "VMLOGS_KUBECONFIG=~/.kube/cfg\nVMLOGS_KUBE_CONTEXT=ctx\nVMLOGS_SSH_HOST=jump\n"
        )
        transport = make_transport(settings, None, 11, {})
        self.assertIsInstance(transport, KubectlTransport)
        self.assertEqual((transport.namespace, transport.service), ("monitoring", "vl:9428"))
        self.assertEqual(
            (transport.kubeconfig, transport.context, transport.ssh_host, transport.timeout),
            ("~/.kube/cfg", "ctx", "jump", 11),
        )


class MainTest(unittest.TestCase):
    def setUp(self):
        self.vl = FakeServer(victorialogs())
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "env"
        self.profile.write_text(f"VMLOGS_TRANSPORT=direct\nVMLOGS_URL={self.vl.url}\n")
        patcher = mock.patch.dict(os.environ, {"VMLOGS_ENV": str(self.profile)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.vl.close()
        self.tmp.cleanup()

    def test_returns_exit_code(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["check"]), 0)
        self.assertIn("записей за 5 минут: 2", out.getvalue())
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(main(["get", "nonsense"]), 1)
        self.assertIn("не похожа", err.getvalue())

    def test_broken_pipe_exits_quietly(self):
        sink = open(os.devnull, "w")
        self.addCleanup(sink.close)

        class ClosedPipe:
            def write(self, text):
                raise BrokenPipeError

            def flush(self):
                raise BrokenPipeError

            def fileno(self):
                return sink.fileno()

        with mock.patch("sys.stdout", ClosedPipe()), mock.patch("os.dup2") as dup2:
            self.assertEqual(main(["check"]), 0)
        dup2.assert_called_once()


class BinTest(unittest.TestCase):
    def test_runs_through_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "vmlogs-cli"
            os.symlink(ROOT / "bin" / "vmlogs-cli", link)
            proc = subprocess.run(
                [sys.executable, str(link), "--version"],
                capture_output=True,
                text=True,
                cwd="/",
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), f"vmlogs-cli {__version__}")

    def test_runs_without_install(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "bin" / "vmlogs-cli"), "--version"],
            capture_output=True,
            text=True,
            cwd="/",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), f"vmlogs-cli {__version__}")


if __name__ == "__main__":
    unittest.main()

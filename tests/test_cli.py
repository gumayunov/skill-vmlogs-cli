import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.fakes import FakeServer, victorialogs
from vmlogs_cli import __version__
from vmlogs_cli.cli import run

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
        self.assertTrue(out.splitlines()[0].endswith("get s1/2026-10-26T07:03:12Z]"))

    def test_out_file(self):
        target = self.dir / "rows.jsonl"
        code, out, _ = self.cli("query", "*", "--out", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(len(target.read_text().splitlines()), 2)
        self.assertIn(f"2 строк → {target}", out)
        self.assertIn("поля: _msg, _time, kubernetes.container_name, level", out)

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
        code, out, _ = self.cli("get", "s1/2026-10-26T07:03:12Z")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["_msg"], "first")
        self.assertEqual(
            self.last_params()["query"],
            "_stream_id:s1 _time:[2026-10-26T07:03:12Z, 2026-10-26T07:03:12Z]",
        )

    def test_get_bad_ref(self):
        code, _, err = self.cli("get", "nonsense")
        self.assertEqual(code, 1)
        self.assertIn("не похожа на <stream_id>/<time>", err)

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
        code, _, _ = self.cli("query", "*", "--env", str(self.profile), "--timeout", "5")
        self.assertEqual(code, 0)


class BinTest(unittest.TestCase):
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

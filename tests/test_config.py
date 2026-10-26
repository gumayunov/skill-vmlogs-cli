import tempfile
import unittest
from pathlib import Path

from vmlogs_cli.config import config_dir, find_env_file, load_settings, parse_env_file
from vmlogs_cli.errors import CliError


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class FindEnvFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.project = self.root / "work" / "shop" / "src"
        self.project.mkdir(parents=True)
        self.env = {"HOME": str(self.home)}

    def tearDown(self):
        self.tmp.cleanup()

    def test_project_env_found_upwards(self):
        expected = write(self.root / "work" / ".vmlogs-cli" / "env", "")
        write(config_dir(self.env) / "env", "")
        self.assertEqual(find_env_file(self.env, self.project), expected)

    def test_falls_back_to_config_dir(self):
        expected = write(self.home / ".config" / "vmlogs-cli" / "env", "")
        self.assertEqual(find_env_file(self.env, self.project), expected)

    def test_vmlogs_env_beats_project(self):
        write(self.root / "work" / ".vmlogs-cli" / "env", "")
        chosen = write(self.root / "other.env", "")
        self.env["VMLOGS_ENV"] = str(chosen)
        self.assertEqual(find_env_file(self.env, self.project), chosen)

    def test_explicit_beats_vmlogs_env(self):
        self.env["VMLOGS_ENV"] = str(write(self.root / "a.env", ""))
        chosen = write(self.root / "b.env", "")
        self.assertEqual(find_env_file(self.env, self.project, str(chosen)), chosen)

    def test_missing_explicit_is_error(self):
        with self.assertRaisesRegex(CliError, "не найден"):
            find_env_file(self.env, self.project, str(self.root / "nope.env"))

    def test_no_profile_is_error(self):
        with self.assertRaisesRegex(CliError, "нет профиля"):
            find_env_file(self.env, self.project)


class ParseEnvFileTest(unittest.TestCase):
    def test_comments_export_and_quotes(self):
        with tempfile.TemporaryDirectory() as d:
            path = write(
                Path(d) / "env",
                "# comment\n\nexport VMLOGS_TRANSPORT=direct\n"
                "VMLOGS_URL=\"http://vl:9428\"\nVMLOGS_HIDE_FIELDS = 'a, b.'\n",
            )
            self.assertEqual(
                parse_env_file(path),
                {
                    "VMLOGS_TRANSPORT": "direct",
                    "VMLOGS_URL": "http://vl:9428",
                    "VMLOGS_HIDE_FIELDS": "a, b.",
                },
            )

    def test_line_without_equals_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = write(Path(d) / "env", "VMLOGS_URL\n")
            with self.assertRaisesRegex(CliError, r"env:1: нужна строка KEY=VALUE"):
                parse_env_file(path)


class LoadSettingsTest(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = write(Path(d) / "env", text)
            return load_settings({"HOME": d}, d, str(path))

    def test_direct(self):
        s = self.load("VMLOGS_TRANSPORT=direct\nVMLOGS_URL=http://vl:9428/\n")
        self.assertEqual((s.transport, s.url), ("direct", "http://vl:9428"))
        self.assertIsNone(s.hide_fields)
        self.assertIsNone(s.context_fields)

    def test_kubectl_over_ssh(self):
        s = self.load(
            "VMLOGS_TRANSPORT=kubectl\nVMLOGS_KUBE_SERVICE=monitoring/vl:9428\nVMLOGS_SSH_HOST=box\n"
        )
        self.assertEqual((s.kube_service, s.ssh_host), ("monitoring/vl:9428", "box"))

    def test_grafana_and_field_lists(self):
        s = self.load(
            "VMLOGS_TRANSPORT=grafana\nVMLOGS_GRAFANA_URL=https://grafana.example.com/\n"
            "VMLOGS_DATASOURCE=Logs\nVMLOGS_HIDE_FIELDS=_stream, k8s.\nVMLOGS_CONTEXT_FIELDS=trace_id\n"
        )
        self.assertEqual(s.grafana_url, "https://grafana.example.com")
        self.assertEqual(s.datasource, "Logs")
        self.assertEqual(s.hide_fields, ["_stream", "k8s."])
        self.assertEqual(s.context_fields, ["trace_id"])

    def test_unknown_key(self):
        with self.assertRaisesRegex(CliError, "неизвестные ключи VMLOGS_UR"):
            self.load("VMLOGS_TRANSPORT=direct\nVMLOGS_URL=x\nVMLOGS_UR=y\n")

    def test_foreign_keys_are_ignored(self):
        s = self.load("VMLOGS_TRANSPORT=direct\nVMLOGS_URL=http://vl:9428\nAWS_REGION=x\n")
        self.assertEqual(s.url, "http://vl:9428")

    def test_url_without_scheme(self):
        with self.assertRaisesRegex(CliError, "VMLOGS_URL.*http://.*https://shop.example.com"):
            self.load("VMLOGS_TRANSPORT=direct\nVMLOGS_URL=shop.example.com\n")

    def test_grafana_url_without_scheme(self):
        with self.assertRaisesRegex(CliError, "VMLOGS_GRAFANA_URL.*https://grafana.example.com"):
            self.load("VMLOGS_TRANSPORT=grafana\nVMLOGS_GRAFANA_URL=grafana.example.com\n")

    def test_url_with_other_scheme(self):
        with self.assertRaisesRegex(CliError, "VMLOGS_URL"):
            self.load("VMLOGS_TRANSPORT=direct\nVMLOGS_URL=ftp://vl.example.com\n")

    def test_ssh_host_looking_like_option(self):
        with self.assertRaisesRegex(CliError, "VMLOGS_SSH_HOST"):
            self.load(
                "VMLOGS_TRANSPORT=kubectl\nVMLOGS_KUBE_SERVICE=monitoring/vl:9428\n"
                "VMLOGS_SSH_HOST=-oProxyCommand=id\n"
            )

    def test_ssh_host_with_whitespace(self):
        with self.assertRaisesRegex(CliError, "VMLOGS_SSH_HOST"):
            self.load(
                "VMLOGS_TRANSPORT=kubectl\nVMLOGS_KUBE_SERVICE=monitoring/vl:9428\n"
                "VMLOGS_SSH_HOST=box id\n"
            )

    def test_bad_transport(self):
        with self.assertRaisesRegex(CliError, "VMLOGS_TRANSPORT должен быть"):
            self.load("VMLOGS_TRANSPORT=http\n")

    def test_missing_required(self):
        with self.assertRaisesRegex(CliError, "нужен VMLOGS_GRAFANA_URL"):
            self.load("VMLOGS_TRANSPORT=grafana\n")

    def test_bad_service(self):
        with self.assertRaisesRegex(CliError, "<namespace>/<service>:<port>"):
            self.load("VMLOGS_TRANSPORT=kubectl\nVMLOGS_KUBE_SERVICE=vl:9428\n")


if __name__ == "__main__":
    unittest.main()

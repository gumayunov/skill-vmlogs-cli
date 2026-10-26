import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from vmlogs_cli.errors import CliError
from vmlogs_cli.login import BrowserLogin

GOOD = "grafana_session=good"


class FakePlaywright:
    """Вместо playwright-cli. mode: silent — сессия жива без окна; headed — только после окна;
    never — вход так и не случился."""

    def __init__(self, mode, open_fails=False):
        self.mode = mode
        self.open_fails = open_fails
        self.headed = False
        self.calls = []

    def __call__(self, cmd, cwd=None, **kwargs):
        assert cmd[0] == "playwright-cli" and cmd[1] == "-s=vmlogs-cli", cmd
        assert cwd, "playwright-cli должен работать во временном каталоге"
        args = cmd[2:]
        self.calls.append(args)
        if args[0] == "open":
            if self.open_fails:
                return subprocess.CompletedProcess(cmd, 1, "", "net::ERR_NAME_NOT_RESOLVED")
            self.headed = "--headed" in args
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if args[:2] == ["--raw", "run-code"]:
            ready = self.mode == "silent" or (self.mode == "headed" and self.headed)
            return subprocess.CompletedProcess(cmd, 0, json.dumps(GOOD if ready else "") + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


class FakeSession:
    def __init__(self, path, refresh_ok=False):
        self.base_url = "https://grafana.example.com"
        self.path = path
        self.refresh_ok = refresh_ok

    def refresh(self):
        return self.refresh_ok

    def valid(self, header):
        return header == GOOD


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class BrowserLoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cookie = self.dir / "cookies" / "grafana.example.com"
        self.err = io.StringIO()

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, playwright, refresh_ok=False, which=lambda name: "/usr/bin/" + name):
        clock = FakeClock()
        BrowserLogin(
            FakeSession(self.cookie, refresh_ok),
            self.dir,
            run=playwright,
            which=which,
            sleep=clock.sleep,
            clock=clock,
            wall=lambda: 0.0,
            err=self.err,
        ).login()

    def test_saved_session_skips_browser(self):
        playwright = FakePlaywright("never")
        self.login(playwright, refresh_ok=True)
        self.assertEqual(playwright.calls, [])
        self.assertFalse(self.cookie.exists())

    def test_missing_playwright(self):
        with self.assertRaisesRegex(CliError, "нужен playwright-cli"):
            self.login(FakePlaywright("silent"), which=lambda name: None)

    def test_silent_login_without_window(self):
        playwright = FakePlaywright("silent")
        self.login(playwright)
        self.assertEqual(self.cookie.read_text().strip(), GOOD)
        opens = [args for args in playwright.calls if args[0] == "open"]
        self.assertEqual(len(opens), 1)
        self.assertNotIn("--headed", opens[0])
        self.assertIn(str(self.dir / "chrome-profile"), opens[0])
        self.assertEqual(playwright.calls[-1], ["close"])

    def test_window_login(self):
        playwright = FakePlaywright("headed")
        self.login(playwright)
        self.assertEqual(self.cookie.read_text().strip(), GOOD)
        self.assertTrue(any("--headed" in args for args in playwright.calls if args[0] == "open"))
        self.assertIn("войди в https://grafana.example.com", self.err.getvalue())
        self.assertEqual(playwright.calls[-1], ["close"])

    def test_window_timeout(self):
        playwright = FakePlaywright("never")
        with self.assertRaisesRegex(CliError, "за 5 минут вход не завершился"):
            self.login(playwright)
        self.assertFalse(self.cookie.exists())
        self.assertEqual(playwright.calls[-1], ["close"])

    def test_open_failure(self):
        playwright = FakePlaywright("silent", open_fails=True)
        with self.assertRaisesRegex(
            CliError, "не открыл https://grafana.example.com/: net::ERR_NAME_NOT_RESOLVED"
        ):
            self.login(playwright)
        self.assertEqual(playwright.calls[-1], ["close"])


if __name__ == "__main__":
    unittest.main()

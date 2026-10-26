import io
import json
import os
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

    def __init__(
        self,
        mode,
        open_fails=False,
        hang=(),
        close_hangs=False,
        headed_open_fails=False,
        bad_captures=0,
        bad_kind="exit",
    ):
        self.mode = mode
        self.open_fails = open_fails
        self.headed_open_fails = headed_open_fails
        self.bad_captures = bad_captures  # первые N захватов cookie — сбойные
        self.bad_kind = bad_kind  # exit — ненулевой код, text — не-JSON вывод
        self.hang = hang  # команды, которые «зависают»: subprocess.TimeoutExpired
        self.close_hangs = close_hangs
        self.kwargs = []
        self.headed = False
        self.calls = []

    def __call__(self, cmd, cwd=None, **kwargs):
        assert cmd[0] == "playwright-cli" and cmd[1] == "-s=vmlogs-cli", cmd
        assert cwd, "playwright-cli должен работать во временном каталоге"
        args = cmd[2:]
        self.calls.append(args)
        self.kwargs.append(kwargs)
        if any(a in self.hang for a in args[:3]) or (args[0] == "close" and self.close_hangs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
        if args[0] == "open":
            if self.open_fails or (self.headed_open_fails and "--headed" in args):
                return subprocess.CompletedProcess(cmd, 1, "", "net::ERR_NAME_NOT_RESOLVED")
            self.headed = "--headed" in args
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if args[:2] == ["--raw", "run-code"]:
            if self.bad_captures > 0:
                self.bad_captures -= 1
                if self.bad_kind == "exit":
                    return subprocess.CompletedProcess(cmd, 1, "", "boom")
                return subprocess.CompletedProcess(cmd, 0, "Error: page crashed\n", "")
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

    def login(
        self,
        playwright,
        refresh_ok=False,
        which=lambda name: "/usr/bin/" + name,
        wall=lambda: 0.0,
    ):
        clock = FakeClock()
        BrowserLogin(
            FakeSession(self.cookie, refresh_ok),
            self.dir,
            run=playwright,
            which=which,
            sleep=clock.sleep,
            clock=clock,
            wall=wall,
            err=self.err,
        ).login()

    def test_saved_session_skips_browser(self):
        playwright = FakePlaywright("never")
        self.login(playwright, refresh_ok=True)
        self.assertEqual(playwright.calls, [])
        self.assertFalse(self.cookie.exists())

    def cookies_db(self, mtime, network=True):
        parts = ("Default", "Network", "Cookies") if network else ("Default", "Cookies")
        db = self.dir.joinpath("chrome-profile", *parts)
        db.parent.mkdir(parents=True)
        db.write_bytes(b"")
        os.utime(db, (mtime, mtime))

    def test_flush_waits_for_cookie_written_after_login(self):
        self.cookies_db(500.0)
        self.login(FakePlaywright("headed"), wall=lambda: 1000.0)
        self.assertIn("профиль не записал cookie", self.err.getvalue())

    def test_flush_returns_when_profile_written(self):
        self.cookies_db(2000.0)
        self.login(FakePlaywright("headed"), wall=lambda: 1000.0)
        self.assertNotIn("профиль не записал cookie", self.err.getvalue())
        self.assertEqual(self.cookie.read_text().strip(), GOOD)

    def test_flush_sees_legacy_cookies_path(self):
        self.cookies_db(2000.0, network=False)
        self.login(FakePlaywright("headed"), wall=lambda: 1000.0)
        self.assertNotIn("профиль не записал cookie", self.err.getvalue())

    def test_login_moment_is_taken_after_capture(self):
        playwright = FakePlaywright("headed")
        events = []
        original = playwright.__call__

        def spy(cmd, cwd=None, **kwargs):
            events.append("capture" if cmd[2:4] == ["--raw", "run-code"] else "pw")
            return original(cmd, cwd=cwd, **kwargs)

        def wall():
            events.append("wall")
            return 1000.0

        self.login(spy, wall=wall)
        self.assertEqual(events.count("wall"), 1)
        self.assertGreater(events.index("wall"), len(events) - 1 - events[::-1].index("capture"))

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

    def test_playwright_gets_devnull_stdin_and_timeout(self):
        playwright = FakePlaywright("silent")
        self.login(playwright)
        for kwargs in playwright.kwargs:
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["timeout"], 60)

    def test_hung_run_code_is_failed_attempt(self):
        playwright = FakePlaywright("silent", hang=("run-code",))
        with self.assertRaisesRegex(CliError, "за 5 минут вход не завершился"):
            self.login(playwright)

    def test_hung_open_is_cli_error(self):
        playwright = FakePlaywright("silent", hang=("open",))
        with self.assertRaisesRegex(CliError, "playwright-cli не ответил за 60 с"):
            self.login(playwright)

    def test_hung_close_does_not_mask_original_error(self):
        playwright = FakePlaywright("silent", open_fails=True, close_hangs=True)
        with self.assertRaisesRegex(CliError, "не открыл"):
            self.login(playwright)

    def test_hung_close_does_not_fail_login(self):
        playwright = FakePlaywright("silent", close_hangs=True)
        self.login(playwright)
        self.assertEqual(self.cookie.read_text().strip(), GOOD)

    def test_headed_open_failure_after_silent_attempt(self):
        playwright = FakePlaywright("never", headed_open_fails=True)
        with self.assertRaisesRegex(CliError, "не открыл https://grafana.example.com/"):
            self.login(playwright)
        self.assertFalse(self.cookie.exists())
        self.assertEqual(playwright.calls[-1], ["close"])

    def test_run_code_nonzero_exit_is_failed_attempt(self):
        playwright = FakePlaywright("silent", bad_captures=3, bad_kind="exit")
        self.login(playwright)
        self.assertEqual(self.cookie.read_text().strip(), GOOD)
        runs = [args for args in playwright.calls if args[:2] == ["--raw", "run-code"]]
        self.assertEqual(len(runs), 4)

    def test_run_code_non_json_is_failed_attempt(self):
        playwright = FakePlaywright("silent", bad_captures=2, bad_kind="text")
        self.login(playwright)
        self.assertEqual(self.cookie.read_text().strip(), GOOD)
        runs = [args for args in playwright.calls if args[:2] == ["--raw", "run-code"]]
        self.assertEqual(len(runs), 3)

    def test_open_failure(self):
        playwright = FakePlaywright("silent", open_fails=True)
        with self.assertRaisesRegex(
            CliError, "не открыл https://grafana.example.com/: net::ERR_NAME_NOT_RESOLVED"
        ):
            self.login(playwright)
        self.assertEqual(playwright.calls[-1], ["close"])


if __name__ == "__main__":
    unittest.main()

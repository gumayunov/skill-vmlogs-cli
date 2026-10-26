"""Сессия Grafana: cookie-файл, проверка, продление без браузера и вход через playwright-cli."""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .errors import CliError
from .transport import host_label, no_redirect_opener


def cookie_file(cfg_dir: Path, base_url: str) -> Path:
    """Cookie одна на хост Grafana: один вход обслуживает все профили с этим хостом."""
    return cfg_dir / "cookies" / host_label(base_url)


def merge_cookies(header: str, set_cookies: list[str]) -> str:
    """Заголовок Cookie с новыми значениями из Set-Cookie; пустое значение — cookie удалена."""
    values: dict[str, str] = {}
    parts = [part.strip() for part in header.split(";")]
    parts += [line.split(";", 1)[0].strip() for line in set_cookies]
    for part in parts:
        name, sep, value = part.partition("=")
        if sep and name:
            values[name] = value
    return "; ".join(f"{name}={value}" for name, value in values.items() if value)


def save_secret(path: Path, text: str) -> None:
    """Атомарная запись с правами 600: клиент не прочтёт половину, чужие не прочтут вовсе."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class GrafanaSession:
    """Cookie сессии Grafana в файле: проверка через /api/user, продление ротацией токена."""

    def __init__(self, base_url: str, path: Path, timeout: int = 30):
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.timeout = timeout

    def saved(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def header(self) -> str:
        header = self.saved()
        if not header:
            raise CliError(f"нет входа в {self.base_url}: выполни vmlogs-cli login")
        return header

    def _call(self, method: str, path: str, header: str):
        data = b"" if method == "POST" else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Cookie", header)
        req.add_header("x-grafana-org-id", "1")
        try:
            with no_redirect_opener().open(req, timeout=self.timeout) as resp:
                return resp.status, resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.headers
        except (OSError, http.client.HTTPException):
            return 0, None

    def valid(self, header: str) -> bool:
        return bool(header) and self._call("GET", "/api/user", header)[0] == 200

    def rotate(self, header: str) -> Optional[str]:
        """Новый заголовок после ротации токена сессии или None, если Grafana не продлила."""
        _, headers = self._call("POST", "/api/user/auth-tokens/rotate", header)
        if headers is None:
            return None
        fresh = merge_cookies(header, headers.get_all("Set-Cookie") or [])
        return fresh if self.valid(fresh) else None

    def refresh(self) -> bool:
        """True — в файле рабочая cookie: сохранённая жива или продлена без браузера."""
        header = self.saved()
        if not header:
            return False
        if self.valid(header):
            return True
        fresh = self.rotate(header)
        if not fresh:
            return False
        save_secret(self.path, fresh)
        return True


PW_SESSION = "vmlogs-cli"
PW_TIMEOUT = 60
SILENT_TIMEOUT = 20
LOGIN_TIMEOUT = 300
FLUSH_TIMEOUT = 45
PW_INSTALL_HINT = "npm install -g @playwright/cli && playwright-cli install-browser"


class BrowserLogin:
    """Вход в Grafana через SSO в настоящем браузере (playwright-cli) с постоянным профилем.

    Порядок: сохранённая сессия или её продление без браузера → браузер без окна (SSO-сессия
    в профиле ещё жива) → окно, где вход проходит человек.
    """

    def __init__(
        self,
        session,
        cfg_dir: Path,
        run=subprocess.run,
        which=shutil.which,
        sleep=time.sleep,
        clock=time.monotonic,
        wall=time.time,
        err=None,
    ):
        self.session = session
        self.profile = cfg_dir / "chrome-profile"
        self.run = run
        self.which = which
        self.sleep = sleep
        self.clock = clock
        self.wall = wall
        self.err = err if err is not None else sys.stderr

    def _say(self, text: str) -> None:
        print(f"vmlogs-cli: {text}", file=self.err)

    def _pw(self, cwd: str, *args: str) -> subprocess.CompletedProcess:
        cmd = ["playwright-cli", f"-s={PW_SESSION}", *args]
        # stdin закрыт: иначе playwright-cli съедает stdin вызывающего цикла; timeout — от зависания.
        return self.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=PW_TIMEOUT,
        )

    def _open(self, cwd: str, headed: bool) -> None:
        url = self.session.base_url + "/"
        args = ["open", *(["--headed"] if headed else []), "--persistent"]
        try:
            proc = self._pw(cwd, *args, "--profile", str(self.profile), url)
        except subprocess.TimeoutExpired:
            raise CliError(f"playwright-cli не ответил за {PW_TIMEOUT} с") from None
        if proc.returncode != 0:
            detail = " ".join((proc.stderr or proc.stdout or "").split())[:300]
            raise CliError(
                f"playwright-cli не открыл {url}: {detail}\n"
                "Проверь VPN и браузер: playwright-cli install-browser"
            )

    def _close(self, cwd: str) -> None:
        # Закрытие — уборка: его сбой не должен перекрывать исходную ошибку.
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            self._pw(cwd, "close")

    def _capture(self, cwd: str) -> Optional[str]:
        # context.cookies(url) не открывает страниц и сам подбирает cookie родительских доменов.
        url = json.dumps(self.session.base_url + "/")
        code = (
            f"async page => {{ const cs = await page.context().cookies({url}); "
            "return cs.map(c => c.name + '=' + c.value).join('; '); }"
        )
        try:
            proc = self._pw(cwd, "--raw", "run-code", code)
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        try:
            header = json.loads(proc.stdout.strip())
        except ValueError:
            return None
        # Cookie появляются раньше, чем закончен вход: годится только та, с которой отвечает API.
        if isinstance(header, str) and self.session.valid(header):
            return header
        return None

    def _wait(self, cwd: str, timeout: int) -> Optional[str]:
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            header = self._capture(cwd)
            if header:
                return header
            self.sleep(1)
        return None

    def _wait_profile_flush(self, since: float) -> None:
        # Chrome пишет cookie в профиль раз в ~30 с, а закрытие из playwright-cli теряет
        # несохранённое — и следующий вход снова открыл бы окно.
        self._say(f"сохраняю сессию в профиле браузера (до {FLUSH_TIMEOUT} с) ...")
        databases = (
            self.profile / "Default" / "Cookies",
            self.profile / "Default" / "Network" / "Cookies",
        )
        deadline = self.clock() + FLUSH_TIMEOUT
        while self.clock() < deadline:
            if any(db.exists() and db.stat().st_mtime > since for db in databases):
                return
            self.sleep(1)
        self._say("профиль не записал cookie — в следующий раз, возможно, снова откроется окно")

    def login(self) -> None:
        if self.session.refresh():
            self._say(
                f"вход в {self.session.base_url} рабочий: сохранённая сессия жива или продлена"
            )
            return
        if not self.which("playwright-cli"):
            raise CliError(f"для входа нужен playwright-cli: {PW_INSTALL_HINT}")
        # playwright-cli складывает служебный каталог .playwright-cli в текущий каталог.
        with tempfile.TemporaryDirectory() as cwd:
            self._close(cwd)
            try:
                self._open(cwd, headed=False)
                header = self._wait(cwd, SILENT_TIMEOUT)
                logged_in_at = None
                if not header:
                    self._close(cwd)
                    minutes = LOGIN_TIMEOUT // 60
                    self._say(
                        f"войди в {self.session.base_url} в открывшемся окне (жду до {minutes} минут) ..."
                    )
                    self._open(cwd, headed=True)
                    header = self._wait(cwd, LOGIN_TIMEOUT)
                    if not header:
                        raise CliError(
                            f"за {minutes} минут вход не завершился — запусти vmlogs-cli login ещё раз"
                        )
                    # Отсчёт от момента захвата cookie: открытие окна Chrome трогает профиль раньше.
                    logged_in_at = self.wall()
                save_secret(self.session.path, header)
                self._say(f"вход сохранён в {self.session.path}")
                if logged_in_at is not None:
                    self._wait_profile_flush(logged_in_at)
            finally:
                self._close(cwd)

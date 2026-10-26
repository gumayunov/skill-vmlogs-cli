"""Сессия Grafana: cookie-файл, проверка, продление без браузера и вход через playwright-cli."""

from __future__ import annotations

import contextlib
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .errors import CliError
from .transport import no_redirect_opener


def cookie_file(cfg_dir: Path, base_url: str) -> Path:
    """Cookie одна на хост Grafana: один вход обслуживает все профили с этим хостом."""
    return cfg_dir / "cookies" / urllib.parse.urlsplit(base_url).netloc


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
        except OSError:
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

"""Транспорты: как дойти до HTTP API VictoriaLogs — напрямую, через kubectl или прокси Grafana.

Контракт для cli: describe() -> str, fetch(endpoint, params) -> Iterator[str] (строки ответа).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

from .errors import CliError

DEFAULT_TIMEOUT = 120
# Тело ответа 400 kubectl показывает только с -v=8, строкой `"message": "<JSON-строка>"`.
_K8S_MESSAGE = re.compile(r'"message":\s*("(?:[^"\\]|\\.)*")')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def no_redirect_opener() -> urllib.request.OpenerDirector:
    """Редирект — это SSO или переезд, а не данные: пусть приходит как HTTPError."""
    return urllib.request.build_opener(_NoRedirect)


def vl_error(code: int, body: str) -> str:
    if code == 400:
        return f"VictoriaLogs не принял запрос: {body[:2000]}"
    return f"HTTP {code}: {body[:1000]}"


def http_error_body(e: urllib.error.HTTPError) -> str:
    return e.read().decode("utf-8", errors="replace").strip()


def iter_lines(resp) -> Iterator[str]:
    """Непустые строки ответа по мере чтения; ответ закрывается в конце."""
    with resp:
        try:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    yield line
        except OSError as e:
            raise CliError(f"ответ оборвался: {e}") from None


class DirectTransport:
    """VictoriaLogs по адресу: port-forward, сервис в кластере, открытый адрес."""

    def __init__(self, url: str, timeout: int = DEFAULT_TIMEOUT):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def describe(self) -> str:
        return f"direct {self.url}"

    def fetch(self, endpoint: str, params: dict[str, str]) -> Iterator[str]:
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{self.url}/select/logsql/{endpoint}", data=data)
        try:
            resp = no_redirect_opener().open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise CliError(vl_error(e.code, http_error_body(e))) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise CliError(f"{self.url} не ответил ({reason}) — проверь адрес и VPN") from None
        return iter_lines(resp)


def _remote_quote(part: str) -> str:
    # `~/` в пути kubeconfig должна раскрыть удалённая оболочка, поэтому тильда — без кавычек.
    if part.startswith("~/"):
        return "~/" + shlex.quote(part[2:])
    return shlex.quote(part)


class KubectlTransport:
    """VictoriaLogs через прокси сервиса в API Kubernetes: `kubectl get --raw`, локально или по ssh."""

    def __init__(
        self,
        service: str,
        kubeconfig: str = "",
        context: str = "",
        ssh_host: str = "",
        timeout: int = DEFAULT_TIMEOUT,
        run=subprocess.run,
    ):
        self.namespace, self.service = service.split("/", 1)
        self.kubeconfig = kubeconfig
        self.context = context
        self.ssh_host = ssh_host
        self.timeout = timeout
        self.run = run

    def describe(self) -> str:
        where = f"ssh {self.ssh_host}: " if self.ssh_host else ""
        return f"kubectl {where}{self.namespace}/{self.service}"

    def command(self, endpoint: str, params: dict[str, str], verbose: bool = False) -> list[str]:
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        path = (
            f"/api/v1/namespaces/{self.namespace}/services/{self.service}"
            f"/proxy/select/logsql/{endpoint}?{query}"
        )
        cmd = ["kubectl"]
        if verbose:
            cmd.append("-v=8")
        if self.kubeconfig:
            # По ssh путь — на удалённой машине, раскрывать ~ здесь нельзя.
            local = os.path.expanduser(self.kubeconfig)
            cmd += ["--kubeconfig", self.kubeconfig if self.ssh_host else local]
        if self.context:
            cmd += ["--context", self.context]
        cmd += ["get", "--raw", path]
        if self.ssh_host:
            # ssh отдаёт команду удалённой оболочке строкой: без кавычек `&` в пути ушёл бы в фон.
            remote = " ".join(_remote_quote(part) for part in cmd)
            return ["ssh", "-o", "BatchMode=yes", self.ssh_host, remote]
        return cmd

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            return self.run(cmd, capture_output=True, timeout=self.timeout)
        except FileNotFoundError:
            raise CliError(f"{cmd[0]} не найден в PATH") from None
        except subprocess.TimeoutExpired:
            raise CliError(f"{cmd[0]} не ответил за {self.timeout} с") from None

    def fetch(self, endpoint: str, params: dict[str, str]) -> Iterator[str]:
        proc = self._run(self.command(endpoint, params))
        if proc.returncode == 0:
            text = proc.stdout.decode("utf-8", errors="replace")
            return iter([line for line in text.splitlines() if line.strip()])
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        if self.ssh_host and proc.returncode == 255:
            raise CliError(f"ssh {self.ssh_host}: {err}\nПроверь доступ: ssh {self.ssh_host} true")
        if "(BadRequest)" in err:
            message = self._bad_request_message(endpoint, params)
            if message:
                raise CliError(vl_error(400, message))
        where = f"kubectl на {self.ssh_host}" if self.ssh_host else "kubectl"
        raise CliError(
            f"{where}: {err}\nПроверь kubeconfig и контекст: kubectl config current-context"
        )

    def _bad_request_message(self, endpoint: str, params: dict[str, str]) -> str:
        """Текст ошибки VictoriaLogs из тела ответа: kubectl показывает его только с -v=8."""
        proc = self._run(self.command(endpoint, params, verbose=True))
        match = _K8S_MESSAGE.search(proc.stderr.decode("utf-8", errors="replace"))
        return json.loads(match.group(1)) if match else ""

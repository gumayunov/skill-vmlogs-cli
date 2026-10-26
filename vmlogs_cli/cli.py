"""Команды vmlogs-cli: разбор аргументов, выбор транспорта, печать и подсказки."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Optional

from . import __version__
from .config import Settings, config_dir, load_settings
from .errors import CliError
from .login import BrowserLogin, GrafanaSession, cookie_file
from .output import DEFAULT_HIDE, field_lines, is_hidden, json_line, short_line, sort_records
from .timewin import window
from .transport import DEFAULT_TIMEOUT, DirectTransport, GrafanaTransport, KubectlTransport

EPILOG = """примеры:
  vmlogs-cli check
  vmlogs-cli query 'kubernetes.pod_namespace:=shop error | stats count() c' --since 6h
  vmlogs-cli query 'kubernetes.pod_namespace:=shop error' --since 6h --limit 20
  vmlogs-cli fields 'kubernetes.pod_namespace:=shop'
  vmlogs-cli values 'kubernetes.pod_namespace:=shop' level
  vmlogs-cli get <stream_id>/<time>

профиль: $VMLOGS_ENV, .vmlogs-cli/env вверх от текущего каталога, ~/.config/vmlogs-cli/env
"""


def build_parser() -> argparse.ArgumentParser:
    conn = argparse.ArgumentParser(add_help=False)
    conn.add_argument("--env", help="файл профиля вместо поиска")
    conn.add_argument("--ds", help="датасорс Grafana (имя или uid) вместо VMLOGS_DATASOURCE")
    conn.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT, help="секунд на запрос (%(default)s)"
    )

    win = argparse.ArgumentParser(add_help=False)
    win.add_argument(
        "--since", default="1h", help="окно назад от сейчас: 15m, 6h, 2d (%(default)s)"
    )
    win.add_argument("--start", help="начало окна: длительность или RFC3339; перебивает --since")
    win.add_argument("--end", help="конец окна: длительность или RFC3339 (по умолчанию — сейчас)")
    win.add_argument("--all-fields", action="store_true", help="не скрывать служебные поля")

    parser = argparse.ArgumentParser(
        prog="vmlogs-cli",
        description="Поиск логов в VictoriaLogs: LogsQL как есть, компактный вывод.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", metavar="<команда>", required=True)

    query = sub.add_parser("query", parents=[conn, win], help="строки логов или результат | stats")
    query.add_argument("logsql")
    query.add_argument(
        "--limit", type=int, default=50, help="максимум строк, последних по времени (%(default)s)"
    )
    query.add_argument("-o", "--output", choices=("short", "json"), default="short")
    query.add_argument(
        "--width",
        type=int,
        default=1000,
        help="обрезать строку (short) или значение (json) длиннее N символов; 0 — не обрезать",
    )
    query.add_argument("--out", help="сырые строки в файл JSONL, на экран — только сводка")

    fields = sub.add_parser("fields", parents=[conn, win], help="имена полей с числом строк")
    fields.add_argument("logsql")

    values = sub.add_parser("values", parents=[conn, win], help="значения поля с числом строк")
    values.add_argument("logsql")
    values.add_argument("field")
    values.add_argument("--limit", type=int, default=50)

    get = sub.add_parser(
        "get", parents=[conn], help="одна запись целиком по ссылке из маркера обрезки"
    )
    get.add_argument("ref", metavar="<stream_id>/<time>")

    sub.add_parser("check", parents=[conn], help="профиль, транспорт и пробный запрос за 5 минут")
    sub.add_parser("login", parents=[conn], help="вход в Grafana (транспорт grafana)")
    sub.add_parser("datasources", parents=[conn], help="датасорсы VictoriaLogs в Grafana")
    return parser


def make_transport(settings: Settings, ds: Optional[str], timeout: int, env: Mapping[str, str]):
    if settings.transport == "direct":
        return DirectTransport(settings.url, timeout)
    if settings.transport == "kubectl":
        return KubectlTransport(
            settings.kube_service,
            settings.kubeconfig,
            settings.kube_context,
            settings.ssh_host,
            timeout,
        )
    session = GrafanaSession(
        settings.grafana_url, cookie_file(config_dir(env), settings.grafana_url)
    )
    return GrafanaTransport(settings.grafana_url, ds or settings.datasource, session, timeout)


class App:
    def __init__(self, args, settings: Settings, transport, env, now: datetime, out, err):
        self.args = args
        self.settings = settings
        self.transport = transport
        self.env = env
        self.now = now
        self.out = out
        self.err = err
        all_fields = getattr(args, "all_fields", False)
        if all_fields:
            self.hide, self.context = [], None
        else:
            hide = settings.hide_fields
            self.hide = list(DEFAULT_HIDE) if hide is None else hide
            self.context = settings.context_fields

    def _print(self, text: str) -> None:
        print(text, file=self.out)

    def _hint(self, text: str) -> None:
        print(f"[{text}]", file=self.err)

    def _params(self, logsql: str) -> dict[str, str]:
        start, end = window(self.args.since, self.args.start, self.args.end, self.now)
        params = {"query": logsql, "start": start}
        if end:
            params["end"] = end
        return params

    def _json(self, endpoint: str, params: dict[str, str]) -> dict:
        return json.loads("".join(self.transport.fetch(endpoint, params)))

    def query(self) -> None:
        a = self.args
        params = {**self._params(a.logsql), "limit": str(a.limit)}
        lines = self.transport.fetch("query", params)
        if a.out:
            count, names = 0, set()
            with open(a.out, "w", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")
                    names.update(json.loads(line))
                    count += 1
            self._print(f"{count} строк → {a.out}")
            self._print(
                "поля: " + ", ".join(sorted(n for n in names if not is_hidden(n, self.hide)))
            )
        else:
            rows = [json.loads(line) for line in lines]
            count = len(rows)
            if a.output == "short":
                for row in sort_records(rows):
                    self._print(short_line(row, a.width, self.hide, self.context))
            else:
                for row in rows:
                    self._print(json_line(row, a.width, self.hide))
        if count == 0:
            self._hint(
                f"0 строк за окно с {params['start']}. Слова ищутся целиком: bulk не найдёт "
                'bulk_operations — нужен bulk* или ~"bulk"; проверь и окно (--since, --start)'
            )
        elif count >= a.limit:
            self._hint(
                f"показаны последние {a.limit} строк окна — есть ещё: сузь запрос "
                "или посчитай через | stats count()"
            )

    def fields(self) -> None:
        data = self._json("field_names", self._params(self.args.logsql))
        lines, hidden = field_lines(data.get("values", []), self.hide)
        for line in lines:
            self._print(line)
        if hidden:
            self._hint(f"скрыто служебных полей: {hidden} (--all-fields покажет)")

    def values(self) -> None:
        a = self.args
        params = {**self._params(a.logsql), "field": a.field, "limit": str(a.limit)}
        values = self._json("field_values", params).get("values", [])
        for item in values:
            self._print(f"{item['hits']}\t{item['value']}")
        if len(values) >= a.limit:
            self._hint(f"показаны {a.limit} значений — есть ещё: подними --limit")

    def get(self) -> None:
        ref = self.args.ref
        stream_id, sep, moment = ref.partition("/")
        if not (sep and stream_id and moment):
            raise CliError(f"ссылка «{ref}» не похожа на <stream_id>/<time> из маркера обрезки")
        query = f"_stream_id:{stream_id} _time:[{moment}, {moment}]"
        lines = self.transport.fetch("query", {"query": query, "limit": "10"})
        rows = [json.loads(line) for line in lines]
        if not rows:
            raise CliError(f"запись {ref} не найдена — проверь, что ссылка скопирована целиком")
        for row in rows:
            self._print(json.dumps(row, ensure_ascii=False, indent=2))

    def check(self) -> None:
        self._print(f"профиль: {self.settings.path}")
        self._print(f"транспорт: {self.transport.describe()}")
        lines = self.transport.fetch("query", {"query": "_time:5m | stats count() c"})
        rows = [json.loads(line) for line in lines]
        self._print(f"записей за 5 минут: {rows[0].get('c', '0') if rows else '0'}")

    def _grafana(self) -> GrafanaTransport:
        if not isinstance(self.transport, GrafanaTransport):
            raise CliError(
                f"{self.args.cmd} — только для VMLOGS_TRANSPORT=grafana, "
                f"а в {self.settings.path} {self.settings.transport}"
            )
        return self.transport

    def login(self) -> None:
        transport = self._grafana()
        BrowserLogin(transport.session, config_dir(self.env), err=self.err).login()
        self.datasources()

    def datasources(self) -> None:
        for uid, name in self._grafana().datasources():
            self._print(f"{uid}\t{name}")


def run(
    argv: list[str],
    env: Mapping[str, str],
    cwd: str,
    now: Optional[datetime] = None,
    out=None,
    err=None,
) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(env, cwd, args.env)
        transport = make_transport(settings, args.ds, args.timeout, env)
        app = App(args, settings, transport, env, now or datetime.now(timezone.utc), out, err)
        getattr(app, args.cmd)()
        return 0
    except CliError as e:
        print(f"vmlogs-cli: {e}", file=err)
        return 1
    except json.JSONDecodeError as e:
        print(f"vmlogs-cli: ответ не похож на JSON VictoriaLogs ({e})", file=err)
        return 1
    except KeyboardInterrupt:
        return 130


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return run(argv if argv is not None else sys.argv[1:], os.environ, os.getcwd())
    except BrokenPipeError:
        # Вывод обрезали (| head): выходим молча, как обычные утилиты.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0

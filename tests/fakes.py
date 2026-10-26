"""Поддельные HTTP-серверы для тестов."""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Как отвечает VictoriaLogs на query с limit: последние записи, от новых к старым.
RECORDS = [
    {
        "_time": "2026-10-26T07:03:14Z",
        "_stream_id": "s1",
        "_msg": "second",
        "kubernetes.container_name": "api",
        "level": "info",
    },
    {
        "_time": "2026-10-26T07:03:12Z",
        "_stream_id": "s1",
        "_msg": "first",
        "kubernetes.container_name": "api",
        "level": "error",
    },
]


class FakeServer:
    """HTTP-сервер на свободном порту в фоновом потоке.

    handle(method, path, params, headers) -> (status, [(заголовок, значение)], тело).
    params — из query string и из тела формы вместе.
    """

    def __init__(self, handle):
        self.requests = []
        requests = self.requests

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, method):
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                params = dict(urllib.parse.parse_qsl(parsed.query))
                params.update(urllib.parse.parse_qsl(body))
                headers = {k.lower(): v for k, v in self.headers.items()}
                requests.append((method, parsed.path, params, headers))
                status, extra, payload = handle(method, parsed.path, params, headers)
                data = payload.encode() if isinstance(payload, str) else payload
                self.send_response(status)
                for key, value in extra:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def victorialogs(prefix=""):
    """Поддельная VictoriaLogs; prefix — путь перед /select (у прокси Grafana он свой)."""
    base = prefix + "/select/logsql/"

    def handle(method, path, params, headers):
        if not path.startswith(base):
            return 404, [], "not found"
        endpoint = path[len(base) :]
        query = params.get("query", "")
        if "bad(" in query:
            return 400, [], "cannot parse `query` arg: unexpected pipe; context: [bad(]"
        if endpoint == "query":
            if "nothing" in query:
                rows = []
            elif "| stats" in query:
                rows = [{"c": str(len(RECORDS))}]
            elif "_stream_id:" in query:
                rows = [RECORDS[1]]
            else:
                rows = RECORDS[: int(params.get("limit", "1000"))]
            return 200, [], "".join(json.dumps(row) + "\n" for row in rows)
        if endpoint == "field_names":
            values = [
                {"value": "_msg", "hits": 2},
                {"value": "_stream", "hits": 2},
                {"value": "level", "hits": 2},
            ]
            return 200, [], json.dumps({"values": values})
        if endpoint == "field_values":
            values = [{"value": "error", "hits": 1}, {"value": "info", "hits": 1}]
            return 200, [], json.dumps({"values": values})
        return 404, [], "unknown endpoint"

    return handle


GOOD = "grafana_session=good"
LOGS_UID = "logs-uid"


def grafana(redirect=False):
    """Поддельная Grafana: вход по cookie GOOD, ротация из grafana_session=old, прокси датасорса."""
    vl = victorialogs(prefix=f"/api/datasources/proxy/uid/{LOGS_UID}")
    unauthorized = (401, [], '{"message":"Unauthorized"}')

    def handle(method, path, params, headers):
        cookie = headers.get("cookie", "")
        if redirect:
            return 302, [("Location", "https://sso.example.com/login")], ""
        if path == "/api/user/auth-tokens/rotate" and method == "POST":
            if "grafana_session=old" not in cookie:
                return unauthorized
            return (
                200,
                [
                    ("Set-Cookie", "grafana_session=good; Path=/; HttpOnly"),
                    ("Set-Cookie", "grafana_session_expiry=99; Path=/"),
                ],
                "",
            )
        if GOOD not in cookie:
            return unauthorized
        if path == "/api/user":
            return 200, [], '{"login":"tester"}'
        if path == "/api/frontend/settings":
            datasources = {
                "Logs": {
                    "uid": LOGS_UID,
                    "name": "Logs",
                    "type": "victoriametrics-logs-datasource",
                },
                "Metrics": {"uid": "vm-uid", "name": "Metrics", "type": "prometheus"},
            }
            return 200, [], json.dumps({"datasources": datasources})
        return vl(method, path, params, headers)

    return handle

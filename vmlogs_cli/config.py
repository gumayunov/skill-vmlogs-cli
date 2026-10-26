"""Профиль: где лежит env-файл и что в нём."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from .errors import CliError

TRANSPORTS = ("direct", "kubectl", "grafana")
KNOWN_KEYS = frozenset(
    {
        "VMLOGS_TRANSPORT",
        "VMLOGS_URL",
        "VMLOGS_KUBE_SERVICE",
        "VMLOGS_KUBECONFIG",
        "VMLOGS_KUBE_CONTEXT",
        "VMLOGS_SSH_HOST",
        "VMLOGS_GRAFANA_URL",
        "VMLOGS_DATASOURCE",
        "VMLOGS_HIDE_FIELDS",
        "VMLOGS_CONTEXT_FIELDS",
    }
)
REQUIRED = {
    "direct": "VMLOGS_URL",
    "kubectl": "VMLOGS_KUBE_SERVICE",
    "grafana": "VMLOGS_GRAFANA_URL",
}
_SERVICE = re.compile(r"^[^/\s]+/[^/:\s]+:[^/:\s]+$")


@dataclass
class Settings:
    path: Path
    transport: str
    url: str = ""
    kube_service: str = ""
    kubeconfig: str = ""
    kube_context: str = ""
    ssh_host: str = ""
    grafana_url: str = ""
    datasource: str = ""
    hide_fields: Optional[list[str]] = None
    context_fields: Optional[list[str]] = None


def config_dir(env: Mapping[str, str]) -> Path:
    """Каталог настроек, cookie и профиля браузера: ~/.config/vmlogs-cli."""
    return Path(env.get("HOME") or Path.home()) / ".config" / "vmlogs-cli"


def _must_exist(path: Path, source: str) -> Path:
    if not path.is_file():
        raise CliError(f"профиль {path} не найден ({source})")
    return path


def find_env_file(env: Mapping[str, str], cwd, explicit: Optional[str] = None) -> Path:
    """Первый найденный: --env, $VMLOGS_ENV, .vmlogs-cli/env вверх от cwd, ~/.config/vmlogs-cli/env."""
    if explicit:
        return _must_exist(Path(explicit).expanduser(), "--env")
    if env.get("VMLOGS_ENV"):
        return _must_exist(Path(env["VMLOGS_ENV"]).expanduser(), "VMLOGS_ENV")
    start = Path(cwd).resolve()
    for directory in (start, *start.parents):
        candidate = directory / ".vmlogs-cli" / "env"
        if candidate.is_file():
            return candidate
    fallback = config_dir(env) / "env"
    if fallback.is_file():
        return fallback
    raise CliError(
        f"нет профиля: не нашёл ни $VMLOGS_ENV, ни .vmlogs-cli/env вверх от {start}, "
        f"ни {fallback}. Пример профиля — README.md, раздел «Профили»"
    )


def parse_env_file(path: Path) -> dict[str, str]:
    """Строки KEY=VALUE; пустые и # пропускаются, `export ` и парные кавычки снимаются."""
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            raise CliError(f"{path}:{number}: нужна строка KEY=VALUE, а не «{raw}»")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _csv(value: Optional[str]) -> Optional[list[str]]:
    if value is None:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


def _check_url(path: Path, key: str, url: str, example: str) -> None:
    if not url:
        return
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise CliError(
            f"{path}: {key} должен начинаться с http:// или https://, например {example}, а не «{url}»"
        )


def load_settings(env: Mapping[str, str], cwd, explicit: Optional[str] = None) -> Settings:
    path = find_env_file(env, cwd, explicit)
    values = parse_env_file(path)
    # Чужие ключи не трогаем: env-файл можно делить с другими инструментами.
    unknown = sorted(key for key in values if key.startswith("VMLOGS_") and key not in KNOWN_KEYS)
    if unknown:
        raise CliError(
            f"{path}: неизвестные ключи {', '.join(unknown)}. Известные: {', '.join(sorted(KNOWN_KEYS))}"
        )
    transport = values.get("VMLOGS_TRANSPORT", "")
    if transport not in TRANSPORTS:
        raise CliError(
            f"{path}: VMLOGS_TRANSPORT должен быть одним из {', '.join(TRANSPORTS)}, а не «{transport}»"
        )
    required = REQUIRED[transport]
    if not values.get(required):
        raise CliError(f"{path}: для VMLOGS_TRANSPORT={transport} нужен {required}")
    service = values.get("VMLOGS_KUBE_SERVICE", "")
    if transport == "kubectl" and not _SERVICE.match(service):
        raise CliError(
            f"{path}: VMLOGS_KUBE_SERVICE должен быть вида <namespace>/<service>:<port>, а не «{service}»"
        )
    for key, example in (
        ("VMLOGS_URL", "https://shop.example.com"),
        ("VMLOGS_GRAFANA_URL", "https://grafana.example.com"),
    ):
        _check_url(path, key, values.get(key, ""), example)
    ssh_host = values.get("VMLOGS_SSH_HOST", "")
    # Хост с «-» ssh принял бы за опцию (-oProxyCommand=… запускает команду). Проверяем
    # только там, где ssh вообще запускается.
    if transport == "kubectl" and (
        ssh_host.startswith("-") or any(ch.isspace() for ch in ssh_host)
    ):
        raise CliError(
            f"{path}: VMLOGS_SSH_HOST — имя или адрес хоста без пробелов и не с «-», а не «{ssh_host}»"
        )
    return Settings(
        path=path,
        transport=transport,
        url=values.get("VMLOGS_URL", "").rstrip("/"),
        kube_service=service,
        kubeconfig=values.get("VMLOGS_KUBECONFIG", ""),
        kube_context=values.get("VMLOGS_KUBE_CONTEXT", ""),
        ssh_host=ssh_host,
        grafana_url=values.get("VMLOGS_GRAFANA_URL", "").rstrip("/"),
        datasource=values.get("VMLOGS_DATASOURCE", ""),
        hide_fields=_csv(values.get("VMLOGS_HIDE_FIELDS")),
        context_fields=_csv(values.get("VMLOGS_CONTEXT_FIELDS")),
    )

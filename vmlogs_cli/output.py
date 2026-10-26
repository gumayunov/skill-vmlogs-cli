"""Вывод: короткие строки для чтения агентом, JSONL, скрытие служебных полей."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Optional

# Служебные поля сборщиков: у каждой записи их десятки, и они в разы больше самого
# сообщения. Окончание «.» — префикс. Меняется VMLOGS_HIDE_FIELDS, показать всё — --all-fields.
DEFAULT_HIDE = (
    "_stream",
    "_stream_id",
    "kubernetes.pod_labels.",
    "kubernetes.pod_annotations.",
    "kubernetes.container_id",
    "kubernetes.pod_ip",
    "kubernetes.pod_node_name",
    "k8s.pod_labels.",
    "k8s.pod_annotations.",
    "k8s.pod_ip",
    "k8s.pod_node_name",
    "k8s.pod_owner",
    "vector.",
    "source_type",
    "file",
    "offset",
    "partition",
    "topic",
    "@timestamp_end",
)
# Разные сборщики называют одно и то же по-разному: берём первое найденное.
CONTAINER_FIELDS = ("kubernetes.container_name", "k8s.container_name")
LEVEL_FIELDS = ("level", "json.level", "severity")


def is_hidden(name: str, hide: Sequence[str]) -> bool:
    return any(name.startswith(h) if h.endswith(".") else name == h for h in hide)


def is_record(row: dict) -> bool:
    """Запись лога, а не строка результата `| stats`."""
    return "_msg" in row


def _flat(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text.replace("\x00", "").replace("\r\n", "⏎").replace("\n", "⏎")


def _first(row: dict, names: Sequence[str]) -> tuple[Optional[str], str]:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return name, _flat(value)
    return None, ""


def _time_key(row: dict) -> tuple[str, str]:
    # Дробная часть разной длины: «…:54Z» раньше «…:54.1Z», а сравнение строк говорит обратное.
    moment = row.get("_time", "")
    return moment[:19], moment[19:].lstrip(".").rstrip("Z").ljust(9, "0")


def sort_records(rows: list[dict]) -> list[dict]:
    """Записи — по возрастанию времени; результат `| stats` — в порядке VictoriaLogs."""
    if rows and all(is_record(row) for row in rows):
        return sorted(rows, key=_time_key)
    return rows


def short_line(
    row: dict, width: int, hide: Sequence[str], context: Optional[Sequence[str]] = None
) -> str:
    """`<время> <контейнер> <УРОВЕНЬ> <сообщение>  k=v …`, длинное — с маркером обрезки."""
    if not is_record(row):
        return " ".join(f"{key}={_flat(value)}" for key, value in row.items())
    container_field, container = _first(row, CONTAINER_FIELDS)
    level_field, level = _first(row, LEVEL_FIELDS)
    used = {"_time", "_msg", container_field, level_field}
    if context is not None:
        tail = [(key, row[key]) for key in context if key in row and key not in used]
    else:
        tail = sorted(
            ((k, v) for k, v in row.items() if k not in used and not is_hidden(k, hide)),
            key=lambda kv: kv[0],
        )
    head = [part for part in (_flat(row.get("_time", "")), container, level.upper()) if part]
    line = " ".join([*head, _flat(row["_msg"])])
    if tail:
        line += "  " + " ".join(f"{key}={_flat(value)}" for key, value in tail)
    if width and len(line) > width:
        marker = f"truncated: {width}/{len(line)} chars"
        if row.get("_stream_id") and row.get("_time"):
            marker += f", get {row['_stream_id']}/{row['_time']}"
        line = f"{line[:width]}… [{marker}]"
    return line


def json_line(row: dict, width: int, hide: Sequence[str]) -> str:
    """Запись JSON без скрытых полей; строки длиннее width — с хвостом `…(+N)`."""
    out = {}
    for key, value in row.items():
        if is_hidden(key, hide):
            continue
        if isinstance(value, str):
            value = value.replace("\x00", "")
            if width and len(value) > width:
                value = f"{value[:width]}…(+{len(value) - width})"
        out[key] = value
    return json.dumps(out, ensure_ascii=False)


def field_lines(values: Iterable[dict], hide: Sequence[str]) -> tuple[list[str], int]:
    """Строки `hits<TAB>имя` по убыванию hits и число скрытых полей."""
    lines, hidden = [], 0
    for item in sorted(values, key=lambda v: (-int(v["hits"]), v["value"])):
        if is_hidden(item["value"], hide):
            hidden += 1
            continue
        lines.append(f"{item['hits']}\t{item['value']}")
    return lines, hidden

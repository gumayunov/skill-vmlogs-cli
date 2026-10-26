# vmlogs-cli — CLI и скил поиска логов в VictoriaLogs

> Дизайн 000 — создан 2026-10-26

## Проблема

Логи кластеров переезжают с ELK на VictoriaLogs, и `kibana-cli` (CLI и скил, которым агенты
ищут логи через Kibana) там больше не помогает. Существующие клиенты VictoriaLogs для агентов
привязаны к одному способу доступа — прокси Grafana с SSO — и к схеме полей одного сборщика:
поля другого сборщика (например, `kubernetes.*` у vlagent) они прячут как служебные.

Нужен один открытый инструмент — CLI и скил для агента, — которым агент ищет логи в любой
VictoriaLogs: напрямую, через `kubectl` (в том числе на сервере по SSH) и через Grafana с SSO.

## Цели

- Открытый репозиторий `github.com/gumayunov/skill-vmlogs-cli`: CLI `vmlogs-cli`, `SKILL.md`,
  `README.md`, тесты, CI.
- Агент получает логи кластера, API которого доступен только на сервере, командой `vmlogs-cli`
  через `ssh <host> kubectl`, без туннелей и ручной настройки под проект.
- Агент получает логи из VictoriaLogs за прокси Grafana с SSO тем же `vmlogs-cli`.
- Профиль выбирается по каталогу, из которого запущен агент.

## Не-цели

- Флаги-фильтры в духе `kibana-cli` (`--service`, `--level`, `--filter`, `--gte` …): LogsQL
  короче флагов и передаётся как есть.
- Фильтр по умолчанию в профиле (аналог `KIBANACLI_DEFAULT_NAMESPACE`).
- Наследование и слияние профилей.
- Запись в VictoriaLogs, `tail`/live-режим, графики.

## Анти-цели

- В репозитории нет хостов, имён датасорсов, кластеров и проектов конкретных окружений — ни в
  файлах, ни в истории коммитов. Всё, что относится к окружениям пользователя (профили,
  выкатка), лежит в `docs/designs/*/local/`, который git игнорирует.
- Секреты (cookie) не лежат в репозиториях и в env-файлах профилей.
- Для запуска не нужны uv, pip-пакеты или виртуальное окружение.
- Вывод по умолчанию не забивает контекст агента: служебные поля скрыты, строки обрезаются.

## Ограничения

- Python только на стандартной библиотеке, `requires-python >= 3.9`; запуск
  `bin/vmlogs-cli` с шебангом `#!/usr/bin/env python3`.
- Разработка — uv: `pyproject.toml`, `ruff`, тесты на `unittest`, прогон на Python 3.9 и 3.13
  (`uv run --python 3.9 …`), CI в GitHub Actions через `astral-sh/setup-uv`.
- Вход через браузер — `playwright-cli` (проверено с 0.1.21: `open --persistent --profile`,
  `--headed`, `--raw run-code` печатает результат JSON-строкой, `close`, сессия `-s=<имя>`).
- VictoriaLogs HTTP API: `/select/logsql/query` (JSONL), `/select/logsql/field_names`,
  `/select/logsql/field_values`; параметры `query`, `start`, `end`, `limit`, `field`.
- Grafana: тип датасорса `victoriametrics-logs-datasource`, прокси
  `/api/datasources/proxy/uid/<uid>/select/logsql/<ручка>`, список датасорсов
  `/api/frontend/settings`, проверка сессии `/api/user`, продление
  `POST /api/user/auth-tokens/rotate`.
- Публикация репозитория на GitHub — public.

## Подходы

1. **Клон `kibana-cli` под VictoriaLogs** — bash + jq, флаги собирают LogsQL. Отвергнут: флаги
   были нужны из-за громоздкого ES DSL, LogsQL короче их; разбор JSONL на jq разрастается.
2. **Доработать существующий клиент VictoriaLogs через Grafana** — добавить транспорты и
   настройку скрытых полей. Отвергнут: он живёт в закрытом репозитории и привязан к одному
   окружению.
3. **Новый открытый инструмент на Python stdlib** — LogsQL как есть, команды `fields`/`values`
   и порядок «сначала посчитать», профили, компактный вывод, маркер обрезки и пресеты из
   `kibana-cli`, три транспорта и свой вход в Grafana. **Выбран.**

## Дизайн

### Устройство

Пакет `vmlogs_cli/` и запускалка `bin/vmlogs-cli` (добавляет корень репозитория в `sys.path`
и вызывает `vmlogs_cli.cli.main`). Модули:

| Модуль | Ответственность |
|---|---|
| `config` | найти env-файл профиля, прочитать `KEY=VALUE`, собрать настройки |
| `timewin` | `--since`/`--start`/`--end` → RFC3339 UTC |
| `transport` | `direct`, `kubectl`, `grafana`: запрос к ручке VictoriaLogs → поток строк ответа |
| `output` | формат `short`/`json`, скрытие служебных полей, обрезка, маркер, сортировка |
| `login` | сессия Grafana: cookie-файл, проверка, продление, вход через `playwright-cli` |
| `cli` | argparse, команды, подсказки и коды выхода |

Entry point `vmlogs-cli = vmlogs_cli.cli:main` в `pyproject.toml` для `uv tool install`.

### Профиль

Env-файл `KEY=VALUE` без секретов. Поиск, первый найденный побеждает: `$VMLOGS_ENV` →
`.vmlogs-cli/env` вверх от cwd → `~/.config/vmlogs-cli/env`. Флаг `--env <файл>` перебивает
поиск. Неизвестный ключ `VMLOGS_*` — ошибка (ловит опечатки).

Типичная раскладка: профиль по умолчанию в `~/.config/vmlogs-cli/env`, профиль другого
окружения — в `.vmlogs-cli/env` общего родительского каталога его проектов (поиск вверх
находит его из любого подкаталога). Какой namespace фильтровать, агент берёт из `CLAUDE.md`
проекта.

### Транспорты (`VMLOGS_TRANSPORT`)

| Транспорт | Настройки | Запрос |
|---|---|---|
| `direct` | `VMLOGS_URL` | `POST <url>/select/logsql/<ручка>` |
| `kubectl` | `VMLOGS_KUBE_SERVICE=<ns>/<svc>:<port>`, необяз. `VMLOGS_KUBECONFIG`, `VMLOGS_KUBE_CONTEXT`, `VMLOGS_SSH_HOST` | `kubectl get --raw /api/v1/namespaces/<ns>/services/<svc>:<port>/proxy/select/logsql/<ручка>?<params>`; с `VMLOGS_SSH_HOST` — `ssh -o BatchMode=yes <host> '<kubectl …>'`, аргументы для удалённой оболочки экранируются `shlex.quote` |
| `grafana` | `VMLOGS_GRAFANA_URL`, `VMLOGS_DATASOURCE` (имя или uid) | `POST <grafana>/api/datasources/proxy/uid/<uid>/select/logsql/<ручка>` с `Cookie` и `x-grafana-org-id: 1`; uid по имени из `/api/frontend/settings` |

Флаг `--ds` перебивает `VMLOGS_DATASOURCE`.

`kubectl get --raw` на ответ 400 печатает только `Error from server (BadRequest)`, без текста
ошибки VictoriaLogs. Тогда `vmlogs-cli` повторяет ту же команду с `-v=8` и берёт текст из
строки `"message": "…"` тела ответа (проверено на VictoriaLogs v1.53.0).

### Вход в Grafana

`vmlogs-cli login`:
1. сохранённая cookie отвечает 200 на `/api/user` — готово;
2. иначе `POST /api/user/auth-tokens/rotate` с ней, новые значения из `Set-Cookie` вливаются
   в заголовок, проверка `/api/user`;
3. иначе `playwright-cli` headless с постоянным профилем `~/.config/vmlogs-cli/chrome-profile`
   — если SSO-сессия в профиле жива, окно не открывается (ждём до 20 секунд);
4. иначе окно `--headed`, ожидание входа до 300 секунд; cookie забираются из контекста
   браузера (`--raw run-code`, `context.cookies(url)`), все cookie хоста одним заголовком;
   после входа ждём до 45 секунд, пока Chrome запишет cookie в профиль.

Cookie — `~/.config/vmlogs-cli/cookies/<хост>`, права 600, одна на хост для всех профилей.
При 401 в запросе `vmlogs-cli` один раз делает шаги 1–2 без браузера и повторяет запрос; не
вышло — ошибка с просьбой выполнить `vmlogs-cli login`.

### Команды

| Команда | Что делает |
|---|---|
| `query '<logsql>'` | строки логов или результат `\| stats` |
| `fields '<logsql>'` | имена полей с числом строк, служебные скрыты |
| `values '<logsql>' <поле>` | значения поля с числом строк |
| `get <stream_id>/<time>` | одна запись целиком (`_stream_id:<id> _time:[<time>, <time>]`), все поля, без обрезки |
| `check` | путь к профилю, транспорт и цель, пробный `_time:5m \| stats count()` |
| `login`, `datasources` | только транспорт `grafana` |

Общие флаги: `--since 1h` (по умолчанию), `--start`/`--end` (длительность или RFC3339),
`--limit 50`, `-o short|json`, `--width 1000`, `--all-fields`, `--out <файл.jsonl>`, `--ds`,
`--env`, `--timeout 120`.

### Вывод

`short`, одна запись — одна строка:

```
2026-10-26T07:03:12.345Z api ERROR connection refused to postgres  kubernetes.pod_name=shop-api-7f9c trace_id=ab12
```

- Время `_time`, контейнер — первое из `kubernetes.container_name`, `k8s.container_name`;
  уровень (в верхнем регистре) — первое из `level`, `json.level`, `severity`; `_msg` с
  переводами строк, заменёнными на `⏎`; затем `k=v` остальных нескрытых полей по алфавиту.
- Скрыты по умолчанию: `_stream`, `_stream_id`, метки и аннотации пода
  (`kubernetes.pod_labels.*`, `k8s.pod_labels.*`, `k8s.pod_annotations.*`), ip, нода, id
  контейнера, `file`, `offset` и подобные. Меняется `VMLOGS_HIDE_FIELDS` (через запятую,
  окончание `.` — префикс). `VMLOGS_CONTEXT_FIELDS` — белый список для хвоста `k=v`.
  `--all-fields` показывает всё.
- Записи (строки с `_msg`) сортируются по `_time` по возрастанию; строки без `_msg` (результат
  `stats`) — `k=v` в порядке VictoriaLogs.
- Строка длиннее `--width` codepoints обрезается с маркером
  `… [truncated: N/M chars, get <stream_id>/<time>]`. Формат маркера — публичный контракт.
- `-o json` — JSONL после скрытия полей в порядке VictoriaLogs; строковые значения длиннее
  `--width` обрезаются с хвостом `…(+N)`, `--width 0` отключает обрезку в обоих форматах.
- `--out` — сырые строки в файл, на экран число строк и список полей.
- Подсказки в stderr: достигнут `--limit` → «показаны последние N», сузить или посчитать
  `| stats count()`; 0 строк → слова ищутся целиком (`bulk*`, `~"bulk"`), проверить окно.

### Ошибки

| Ситуация | Поведение |
|---|---|
| 400 `cannot parse` | текст ошибки VictoriaLogs целиком, с `context:` |
| 401 от Grafana | тихое продление и повтор; не вышло — «выполни `vmlogs-cli login`» |
| 3xx от Grafana | Grafana переехала или SSO-редирект: показать `Location` |
| ненулевой выход `kubectl` | его stderr и подсказка про kubeconfig и контекст |
| `ssh` вернул 255 | stderr ssh и подсказка проверить `ssh <host> true` |
| сеть не отвечает | подсказка про адрес и VPN |
| нет профиля | где искали и ссылка на раздел «Профили» в `README.md` |

Ошибки — в stderr одной-двумя строками без трейсбэка, код выхода 1.

### Скил и документация

`SKILL.md`: порядок работы (`check` → `| stats count()` → строки → сужение → `get` для
обрезанных), шпаргалка LogsQL, поиск полей через `fields`/`values`, маркеры вывода, таблица
«если не получилось», пресеты проекта `.vmlogs-cli/*.sh` по шаблону `presets/TEMPLATE.sh`,
анти-паттерны. `README.md` — установка (симлинк в `~/.claude/skills`, `uv tool install`),
профили трёх транспортов, вход, разработка. Примеры — только с `example.com` и вымышленными
именами.

### Тесты

`unittest`: формат и CLI — на поддельной VictoriaLogs (`http.server`); `direct` и `grafana` —
на локальном `http.server`, включая 401 → продление → повтор и 3xx; `kubectl` — на поддельном
`run` и на заглушках `kubectl` и `ssh` в `PATH` (экранирование аргументов через настоящий
`sh -c`); вход — на поддельном `run` вместо `playwright-cli`. CI: Python 3.9 и 3.13,
`ruff check`, `ruff format --check`.

### Выкатка

Подключение в окружениях пользователя и публикация — локальный план
`local/phase-000-02.md` (не коммитится). Перед публикацией — проверка файлов и истории на
названия окружений по списку из `local/`.

## Оставлено исполнителю

- Внутреннее устройство модулей в пределах интерфейсов плана.
- Точный список скрытых полей по умолчанию — по живым записям окружений (правка в профиле
  через `VMLOGS_HIDE_FIELDS`, если умолчание не подходит).

## Открытые вопросы

Нет. Проверено на VictoriaLogs v1.53.0 2026-10-26:

- `/select/logsql/query` с `limit` без `| sort` возвращает **последние** N записей окна,
  от новых к старым. Поэтому `short` переворачивает их по возрастанию.
- `_stream_id:<id> _time:[t, t]` с временем в наносекундах находит ровно ту запись.
- Относительное `start=5m` VictoriaLogs не понимает как «5 минут назад» — `vmlogs-cli` сам
  переводит окно в RFC3339 UTC.
- Прокси сервиса `/api/v1/namespaces/<ns>/services/<svc>:<port>/proxy/...` через
  `kubectl get --raw` работает, в том числе `ssh <host> 'kubectl get --raw …'`.

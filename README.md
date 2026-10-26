# vmlogs-cli

CLI и скил для агентов: поиск логов в VictoriaLogs. Запрос — [LogsQL](https://docs.victoriametrics.com/victorialogs/logsql/)
как есть, вывод — одна компактная строка на запись, длинные строки обрезаются с маркером, по
которому достаётся полная запись. Три способа дойти до VictoriaLogs: напрямую, через `kubectl`
(в том числе на сервере по ssh, когда API кластера снаружи недоступен) и через прокси Grafana
с SSO.

Python 3.9+, только стандартная библиотека. Инструкция для агента — [`SKILL.md`](SKILL.md).

## Установка

Как скил Claude Code и команда в PATH:

```bash
git clone https://github.com/gumayunov/skill-vmlogs-cli.git ~/src/skill-vmlogs-cli
ln -s ~/src/skill-vmlogs-cli ~/.claude/skills/vmlogs-cli
ln -s ~/src/skill-vmlogs-cli/bin/vmlogs-cli ~/.local/bin/vmlogs-cli
```

Только команда:

```bash
uv tool install git+https://github.com/gumayunov/skill-vmlogs-cli
```

## Профили

Профиль — env-файл `KEY=VALUE` без секретов. Первый найденный побеждает: `--env <файл>`,
`$VMLOGS_ENV`, `.vmlogs-cli/env` вверх от текущего каталога, `~/.config/vmlogs-cli/env`.
Удобно держать профиль по умолчанию в `~/.config/vmlogs-cli/env`, а профиль другого окружения —
в `.vmlogs-cli/env` общего каталога его проектов.

Напрямую (port-forward, сервис в кластере, открытый адрес):

```bash
VMLOGS_TRANSPORT=direct
VMLOGS_URL=http://localhost:9428
```

Через `kubectl` — прокси сервиса в API Kubernetes:

```bash
VMLOGS_TRANSPORT=kubectl
VMLOGS_KUBE_SERVICE=logging/victorialogs:9428   # <namespace>/<service>:<port>
VMLOGS_KUBECONFIG=~/.kube/config                # необязательно
VMLOGS_KUBE_CONTEXT=prod                        # необязательно
```

Через `kubectl` на сервере по ssh — когда API кластера доступен только оттуда (туннель не нужен):

```bash
VMLOGS_TRANSPORT=kubectl
VMLOGS_SSH_HOST=myserver                        # хост или алиас из ~/.ssh/config
VMLOGS_KUBE_SERVICE=logging/victorialogs:9428
```

`ssh` запускается с `BatchMode=yes`: вход по ключу должен работать без вопросов.

Через Grafana с SSO:

```bash
VMLOGS_TRANSPORT=grafana
VMLOGS_GRAFANA_URL=https://grafana.example.com
VMLOGS_DATASOURCE=VictoriaLogs                  # имя или uid; флаг --ds перебивает
```

Вывод настраивается в любом профиле:

```bash
VMLOGS_HIDE_FIELDS=_stream,_stream_id,k8s.pod_labels.   # скрытые поля; окончание «.» — префикс
VMLOGS_CONTEXT_FIELDS=kubernetes.pod_name,trace_id      # какие поля печатать после сообщения
```

`VMLOGS_HIDE_FIELDS` **заменяет** список по умолчанию, а не дополняет его. По умолчанию
скрыты: `_stream`, `_stream_id`, `kubernetes.pod_labels.`, `kubernetes.pod_annotations.`,
`kubernetes.container_id`, `kubernetes.pod_ip`, `kubernetes.pod_node_name`, `k8s.pod_labels.`,
`k8s.pod_annotations.`, `k8s.pod_ip`, `k8s.pod_node_name`, `k8s.pod_owner`, `vector.`,
`source_type`, `file`, `offset`, `partition`, `topic`, `@timestamp_end`. Поля `file`, `topic`,
`partition`, `offset` скрываются по точному имени и могут скрыть одноимённые поля приложения —
тогда задай свой список. Показать всё разово — `--all-fields`.

Каталог `.vmlogs-cli/` — доверенная конфигурация, как `.envrc`: профиль запускает ssh и
kubectl, пресеты — скрипты. Не запускай vmlogs-cli в чужих непроверенных клонах.

Проверка: `vmlogs-cli check`.

## Вход в Grafana

```bash
vmlogs-cli login
```

1. Сохранённая cookie жива или продлевается ротацией токена — браузер не нужен.
2. Иначе [`playwright-cli`](https://www.npmjs.com/package/@playwright/cli) открывает браузер
   без окна с постоянным профилем `~/.config/vmlogs-cli/chrome-profile`: если SSO-сессия в нём
   жива, вход проходит молча.
3. Иначе открывается окно, вход проходит человек (до 5 минут).

Cookie хранится в `~/.config/vmlogs-cli/cookies/<хост>` с правами 600, одна на хост Grafana.
Когда запрос получает 401, `vmlogs-cli` один раз продлевает сессию без браузера и повторяет
запрос. Установить `playwright-cli`:
`npm install -g @playwright/cli && playwright-cli install-browser`.

## Команды

```bash
vmlogs-cli check                                          # профиль, транспорт, записи за 5 минут
vmlogs-cli query '<logsql>' [--since 1h] [--limit 50]     # строки или результат | stats
vmlogs-cli query '<logsql>' -o json                       # JSONL
vmlogs-cli query '<logsql>' --limit 5000 --out rows.jsonl # выгрузка в файл
vmlogs-cli fields '<logsql>'                              # имена полей с числом строк
vmlogs-cli values '<logsql>' <поле>                       # значения поля с числом строк
vmlogs-cli get <stream_id>/<time>                         # запись целиком по маркеру обрезки
vmlogs-cli datasources                                    # датасорсы логов в Grafana
```

Окно: `--since 15m|6h|2d` или `--start`/`--end` (длительность или RFC3339). Остальное —
`vmlogs-cli <команда> --help`.

## Разработка

```bash
uv run python -m unittest discover -s tests -v
uv run --python 3.9 python -m unittest discover -s tests
uv run ruff check . && uv run ruff format --check .
```

Дизайн и планы — `docs/designs/`.

## Лицензия

MIT

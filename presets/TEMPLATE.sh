#!/usr/bin/env bash
# <что показывает пресет — одна строка>
# Пример: .vmlogs-cli/CHANGE-ME.sh '"timeout"' --since 30m --limit 20
# Первый аргумент — дополнительный фильтр LogsQL (можно ''), остальные — флаги vmlogs-cli.
set -euo pipefail
extra=${1-}
shift || true
exec "${VMLOGS_CLI:-vmlogs-cli}" query \
  "kubernetes.pod_namespace:=CHANGE-ME kubernetes.container_name:=CHANGE-ME ${extra}" "$@"

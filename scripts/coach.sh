#!/usr/bin/env bash
# Personal Docker lifecycle. Requires Docker Engine + Compose; no host Python.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
else
  echo '请先安装 Docker Engine 和 Docker Compose。' >&2
  exit 1
fi
compose+=(--env-file "${COACH_ENV_FILE:-.env}")
command="${1:-help}"
task_tmp=''
locked=false
resume_services=()
resume_count=0
cleanup() {
  if [[ $resume_count -gt 0 ]]; then "${compose[@]}" start "${resume_services[@]}" >/dev/null; fi
  if [[ -n "$task_tmp" ]]; then rm -rf "$task_tmp"; fi
  if $locked; then rmdir .coach-maintenance.lock; fi
}
trap cleanup EXIT
lock() {
  if ! mkdir .coach-maintenance.lock 2>/dev/null; then
    echo '已有维护任务；若前次进程意外结束，请确认无维护进程后删除 .coach-maintenance.lock。' >&2
    exit 1
  fi
  locked=true
}
case "$command" in
  init)
    if [[ -e .env ]]; then echo '.env 已存在，保留当前配置。'; exit 0; fi
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == POSTGRES_PASSWORD=* ]]; then
        printf 'POSTGRES_PASSWORD=%s\n' "$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
      else printf '%s\n' "$line"; fi
    done < .env.docker.example > .env
    chmod 600 .env
    echo '已创建私有 .env 和随机数据库口令。默认是离线演示；真实生成请编辑模型配置。'
    ;;
  up)
    lock
    "${compose[@]}" build api
    "${compose[@]}" up -d --no-build --wait --wait-timeout 120
    local_address="$("${compose[@]}" port api 8000)"
    echo "已启动，浏览器打开 http://${local_address}/ 。"
    ;;
  down) lock; "${compose[@]}" down ;;
  status) "${compose[@]}" ps ;;
  logs) "${compose[@]}" logs --tail 80 api worker migrate ;;
  backup)
    lock
    target="${2:-backups/coach-$(date -u +%Y%m%dT%H%M%SZ).tar.gz}"
    mkdir -p "$(dirname "$target")"
    if [[ -e "$target" ]]; then echo '目标备份已存在，拒绝覆盖。' >&2; exit 1; fi
    mkdir -p backups
    task_tmp="$(mktemp -d "$PWD/backups/.work-backup.XXXXXX")"
    running_services="$("${compose[@]}" ps --status running --services)"
    while IFS= read -r service; do
      case "$service" in api|worker) resume_services+=("$service"); resume_count=$((resume_count + 1));; esac
    done <<< "$running_services"
    "${compose[@]}" stop -t 30 api worker
    # shellcheck disable=SC2016 # Variables belong to the PostgreSQL container.
    "${compose[@]}" exec -T postgres sh -c 'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' > "$task_tmp/database.dump"
    "${compose[@]}" run --rm --no-deps --user 0 --entrypoint python -v "$task_tmp:/backup" api -m scripts.archive_tools pack /backup
    cp "$task_tmp/backup.tar.gz" "$target"
    chmod 600 "$target"
    echo "一致备份已保存：${target}。独立删除记录保留在 journal volume，未写入可回滚备份。"
    ;;
  restore)
    lock
    if [[ -z "${2:-}" || ! -f "$2" ]]; then echo '用法：scripts/coach.sh restore <备份.tar.gz>' >&2; exit 1; fi
    mkdir -p backups
    task_tmp="$(mktemp -d "$PWD/backups/.work-restore.XXXXXX")"
    cp "$2" "$task_tmp/backup.tar.gz"
    # Stop writes before checking anything. Failed restores never reopen services.
    "${compose[@]}" stop -t 30 api worker
    "${compose[@]}" up -d --no-build postgres
    "${compose[@]}" run --rm --no-deps --user 0 --entrypoint python -v "$task_tmp:/backup" api -m scripts.archive_tools verify /backup/backup.tar.gz
    # shellcheck disable=SC2016 # Variables belong to the PostgreSQL container.
    "${compose[@]}" exec -T postgres sh -c 'exec pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --clean --if-exists --no-owner --exit-on-error' < "$task_tmp/verified/database.dump"
    "${compose[@]}" run --rm --no-deps migrate
    "${compose[@]}" run --rm --no-deps --user 0 --entrypoint python -v "$task_tmp:/backup" api -m scripts.archive_tools restore-data /backup/verified
    "${compose[@]}" up -d --no-build api worker
    echo '恢复完成；已校验文件并重放删除记录。请查看服务健康状态。'
    ;;
  journal-backup)
    lock
    target="${2:-backups/journal-$(date -u +%Y%m%dT%H%M%SZ).tar.gz}"
    mkdir -p "$(dirname "$target")" backups
    if [[ -e "$target" ]]; then echo '目标文件已存在，拒绝覆盖。' >&2; exit 1; fi
    task_tmp="$(mktemp -d "$PWD/backups/.work-journal.XXXXXX")"
    "${compose[@]}" run --rm --no-deps --user 0 --entrypoint python -v "$task_tmp:/backup" api -m scripts.journal_tools export /backup/journal.tar.gz
    cp "$task_tmp/journal.tar.gz" "$target"
    chmod 600 "$target"
    echo "独立删除记录已保存：${target}。请在每次删除后更新其异地副本。"
    ;;
  journal-restore)
    lock
    if [[ -z "${2:-}" || ! -f "$2" ]]; then echo '用法：scripts/coach.sh journal-restore <删除记录.tar.gz>' >&2; exit 1; fi
    mkdir -p backups
    task_tmp="$(mktemp -d "$PWD/backups/.work-journal-import.XXXXXX")"
    cp "$2" "$task_tmp/journal.tar.gz"
    "${compose[@]}" stop -t 30 api worker
    "${compose[@]}" up -d --no-build postgres
    "${compose[@]}" run --rm --no-deps --user 0 --entrypoint python -v "$task_tmp:/backup" api -m scripts.journal_tools import /backup/journal.tar.gz
    ;;
  *) echo '用法：scripts/coach.sh {init|up|status|logs|down|backup [文件]|restore 文件|journal-backup [文件]|journal-restore 文件}' ;;
esac

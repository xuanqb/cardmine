#!/usr/bin/env bash
# 生产环境更新：拉取最新代码并重建应用镜像，数据卷不受影响。
# 若代码相对上次部署没有变动则跳过构建与重启；加 --force 可强制重新部署。
set -euo pipefail

cd "$(dirname "$0")"

FORCE=0
UNKNOWN=""
for arg in "$@"; do
  case "$arg" in
    -f | --force) FORCE=1 ;;
    -h | --help)
      printf '用法：%s [--force]\n  -f, --force  代码无变动时也强制重建并重启\n' "$0"
      exit 0
      ;;
    *)
      UNKNOWN="$arg"
      break
      ;;
  esac
done

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  log "$*"
  exit 1
}

[ -z "$UNKNOWN" ] || die "未知参数：${UNKNOWN}（可用参数：--force）"

command -v git >/dev/null 2>&1 || die "未找到 git，无法更新代码"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "未找到 docker compose，请先安装 Docker Compose"
fi

docker info >/dev/null 2>&1 \
  || die "无法访问 Docker，请确认 Docker 已启动且当前用户有权限"

app_running() {
  "${COMPOSE[@]}" ps --services --filter status=running 2>/dev/null \
    | grep -x app >/dev/null
}

if [ -n "$(git status --porcelain)" ]; then
  die "工作区有未提交的修改，请先处理后再部署"
fi

DEPLOY_REV_FILE=".git/cardmine-deploy-rev"

log "拉取最新代码"
git pull --ff-only
REV=$(git rev-parse HEAD)
SHORT_REV=$(git rev-parse --short HEAD)

if [ "$FORCE" -eq 0 ] && [ -f "$DEPLOY_REV_FILE" ] \
  && [ "$(cat "$DEPLOY_REV_FILE")" = "$REV" ] && app_running; then
  log "代码无变动（${SHORT_REV}），跳过构建与重启；如需强制部署请加 --force"
  exit 0
fi

log "重建应用镜像"
"${COMPOSE[@]}" build --pull app

log "重建并启动容器"
"${COMPOSE[@]}" up -d --remove-orphans

log "当前状态"
"${COMPOSE[@]}" ps

printf '%s\n' "$REV" >"$DEPLOY_REV_FILE"

log "完成"

#!/usr/bin/env bash
# 生产环境更新：拉取最新代码并重建应用镜像，数据卷不受影响。
set -euo pipefail

cd "$(dirname "$0")"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

die() {
  log "$*"
  exit 1
}

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

if [ -n "$(git status --porcelain)" ]; then
  die "工作区有未提交的修改，请先处理后再部署"
fi

log "拉取最新代码"
git pull --ff-only

log "重建应用镜像"
"${COMPOSE[@]}" build --pull app

log "重建并启动容器"
"${COMPOSE[@]}" up -d --remove-orphans

log "当前状态"
"${COMPOSE[@]}" ps

log "完成"

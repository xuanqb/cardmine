# cardmine

面经闪卡系统：定时抓取牛客 RSS，AI 拆题与语义去重，人工补全 Markdown 答案，推送到无头 Anki 容器并同步 AnkiWeb。

- 技术栈：FastAPI + SQLite(WAL) + sqlite-vec + 原生 JavaScript
- 部署形态：Docker Compose 两个容器（主应用 + `anki-connect-server`）
- 适用环境：NAS / 常驻 Linux，数据目录必须位于本地磁盘

> 部署执行者请按 **2 → 3 → 4 → 5 → 10** 节顺序操作。

---

## 1. 目录与数据

```
.
├── docker-compose.yml
├── .env.example          # 环境变量模板，复制为 .env
├── app/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── cardmine/         # 后端
│   └── web/              # 前端静态页面与 vendor 资源
└── data/                 # 运行时生成，已在 .gitignore 中
    ├── app/              # 主应用数据（挂载到容器 /data）
    │   ├── cardmine.db
    │   ├── media/
    │   ├── imports/
    │   └── backups/anki/ # 同步前 Anki 备份，保留 7 份
    └── anki/             # Anki 集合与媒体（挂载到 Anki 容器 /data/anki）
        ├── collection.anki2
        └── collection.media/
```

---

## 2. 部署前准备

1. 安装 Docker 与 Docker Compose v2（`docker compose version` 可用）。
2. 准备两个 **NAS 本地磁盘目录**，不要使用 SMB/NFS：
   - 主应用数据目录，例如 fnOS `/vol1/1000/docker/cardmine/app`、群晖 `/volume1/docker/cardmine/app`
   - Anki 数据目录，例如 fnOS `/vol1/1000/docker/cardmine/anki`
   - 不同 NAS 的 Docker 根目录不同，按实际路径替换，不要照抄示例
3. 准备局域网端口：主应用 `8080`、AnkiConnect `8765`（均可通过 `.env` 修改）。
4. 如需与手机/桌面 Anki 同步，准备 AnkiWeb 账号密码。
5. 如 AnkiWeb 上没有现成集合，准备已有的 `.apkg` 文件。

---

## 3. 部署

```bash
git clone git@github.com:xuanqb/cardmine.git
cd cardmine
cp .env.example .env
```

编辑 `.env`：

```dotenv
# AnkiWeb 凭据：只注入 Anki 容器，不写入数据库
ANKIWEB_USER=你的邮箱
ANKIWEB_PASS=你的密码

# 必须使用 NAS 本地磁盘的绝对路径，按实际 Docker 根目录替换
APP_DATA_PATH=/vol1/1000/docker/cardmine/app
ANKI_DATA_PATH=/vol1/1000/docker/cardmine/anki

APP_PORT=8080
ANKI_PORT=8765

# 首次全量同步完成前保持 false，完成后再改 true
ANKI_FAIL_ON_FULL_SYNC_CONFLICT=false
```

创建目录并启动（按上面填写的绝对路径替换）：

```bash
mkdir -p /vol1/1000/docker/cardmine/app /vol1/1000/docker/cardmine/anki
docker compose up -d --build
docker compose ps
```

如果使用默认相对路径，则改为 `mkdir -p data/app data/anki`。

检查主应用：

```bash
curl -s http://127.0.0.1:8080/api/health
```

期望返回：

```json
{"status":"ok","app_version":"0.1.0","db":"ok","schema_version":4,"anki_url":"http://anki-connect-server:8765/api"}
```

> `anki-connect-server` 默认映射宿主机 `${ANKI_PORT:-8765}` 端口，方便在局域网内直连排障。该端口无鉴权，只应暴露给可信局域网，不要映射到公网；不需要时可在 `docker-compose.yml` 中注释掉 `ports`。

---

## 4. 首次接入 Anki

主应用地址：`http://NAS_IP:8080`，先进入 **设置** 页，确认：

- 容器地址：`http://anki-connect-server:8765/api`
- 模型名：`Basic`
- 模型字段必须正好是 `Front`、`Back`

### 4.1 AnkiWeb 已有集合

1. 确认 `.env` 已填写 `ANKIWEB_USER` / `ANKIWEB_PASS`，并已 `docker compose up -d`。
2. 设置页点击 **检测连接**。
3. 点击 **同步 AnkiWeb**，等待完成；空集合应下拉 AnkiWeb 上的集合与复习记录。
4. 查看 `docker compose logs -f anki-connect-server`，确认没有全量同步冲突。
5. 同步成功后，把 `.env` 中的 `ANKI_FAIL_ON_FULL_SYNC_CONFLICT` 改为 `true`，执行：

```bash
docker compose up -d
```

### 4.2 AnkiWeb 为空或不用 AnkiWeb

把已有 apkg 导入 Anki 容器：

```bash
docker cp Hollis八股文.apkg cardmine-anki:/data/anki/import.apkg

docker exec -i cardmine-app python - <<'PY'
import json, urllib.request
body = {
    "action": "importPackage",
    "version": 6,
    "params": {"path": "/data/anki/import.apkg"},
}
req = urllib.request.Request(
    "http://anki-connect-server:8765/api",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json"},
)
print(urllib.request.urlopen(req).read().decode())
PY

docker exec cardmine-anki rm -f /data/anki/import.apkg
```

然后设置页 **检测连接**，确认能看到牌组与笔记。若要把本地集合上传到 AnkiWeb，再点击 **同步 AnkiWeb**，成功后再启用冲突保护。

### 4.3 把 apkg 导入 cardmine

打开 `/import`，上传同一个 apkg。导入完成后会得到模块树、卡片、背面内容与媒体文件。

然后到 **设置 → 接管匹配**，把系统卡片与 Anki 已有笔记按「正面归一化文本」匹配。结果中的 `matched` 应接近卡片总数。

---

## 5. 首次配置与使用

### 5.1 设置页

| 配置 | 说明 |
|---|---|
| RSS 源 | **默认为空**，必须填写实际 RSS 地址 |
| 抓取间隔 | 默认 60 分钟，最小 5 分钟 |
| Chat Base URL / API Key | OpenAI 兼容接口，用于切题与重复复核 |
| Chat 模型 | 切题与判断所用模型 |
| Embedding Base URL / API Key | 可选；留空则复用 Chat 的地址和密钥，用于向量去重 |
| Embedding 模型 | 向量模型，可与 Chat 使用不同服务商 |
| 向量维度 | 必须与 embedding 模型输出一致，默认 1024 |
| 去重高阈值 | 默认 0.92，达到后直接归并 |
| 去重低阈值 | 默认 0.82，中间区间进入待确认 |
| Anki 容器地址 | 固定为 `http://anki-connect-server:8765/api` |
| 自动同步 AnkiWeb | 推送结束后自动 `sync + syncMedia` |
| 全量同步冲突保护 | 首次同步完成后建议开启 |

保存设置后会自动创建/更新 RSS 源，无需重启。回到仪表盘点 **立即抓取** 即可开始。

> Chat 和 Embedding 可以指向不同服务商：例如 Chat 用网关的 `deepseek-*`，Embedding 单独填硅基流动的 `https://api.siliconflow.cn/v1`、对应 Key 与 `BAAI/bge-m3`。只填 Chat、Embedding 留空时，会自动复用 Chat 的地址和密钥。

> AI 新建模块时会优先挂到卡片量最大的现有顶层模块下，不会创建平行顶层分组；应用启动时会自动把已有的 AI 顶层模块归入该分组。

### 5.2 日常流程

1. **仪表盘**：查看抓取状态、漏抓告警、草稿数、待确认数、同步失败、AI 失败、媒体缺失、Anki 连通。
2. **面经**：查看抓取到的文章与 AI 拆出的问题提及。
3. **审核**：处理待确认重复（合并为变体 / 保持独立 / 忽略），支持批量与撤销；可执行全库语义去重扫描。
4. **卡片**：筛选 `draft`，补全 Markdown 背面，改为 `ready`；支持代码块高亮与实时预览。
5. **推送**：设置页 **立即推送**，只推送 `ready` 且内容哈希变化的卡片；推送前自动备份 Anki，推送后按开关自动同步 AnkiWeb。
6. **手机端**：AnkiDroid 打开/关闭时自动同步，无需额外操作。

> 抓取任务结束后会自动执行一轮推送；新抓到的卡片是 `draft`，不会被推送。

### 5.3 导出与备份

- `/export`：按模块/状态导出 `.apkg` 或 CSV（`Front, Back, Deck`），并查看 Anki 状态与最近同步失败。
- `/export` 或 `/settings`：下载 SQLite 一致性备份与媒体清单。
- Anki 同步前备份：集合主库与 `-wal`/`-shm` 按文件复制，媒体目录做快照，保存在宿主机 `APP_DATA_PATH/backups/anki/`，自动保留最近 7 份。

`.apkg` 可手动导入 Anki，作为容器异常时的兜底。

---

## 6. 运维命令

```bash
# 状态
docker compose ps

# 日志
docker compose logs -f app
docker compose logs -f anki-connect-server

# 重启主应用
docker compose restart app

# 更新代码
git pull
docker compose up -d --build

# 停止
docker compose down
```

数据库迁移在应用启动时按 `PRAGMA user_version` 自动执行。

整机备份建议先停容器，再复制整个数据目录：

```bash
docker compose stop
tar czf cardmine-backup-$(date +%F).tar.gz \
  -C /vol1/1000/docker/cardmine app anki
docker compose start
```

---

## 7. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_PORT` | `8080` | 主应用宿主机端口 |
| `APP_DATA_PATH` | `./data/app` | 主应用数据目录，必须本地磁盘 |
| `ANKI_DATA_PATH` | `./data/anki` | Anki 集合与媒体目录 |
| `ANKIWEB_USER` | 空 | AnkiWeb 邮箱 |
| `ANKIWEB_PASS` | 空 | AnkiWeb 密码 |
| `ANKI_FAIL_ON_FULL_SYNC_CONFLICT` | `false` | 全量同步冲突保护，首次同步后改 `true` |
| `ANKI_PORT` | `8765` | AnkiConnect 宿主机端口，无鉴权，仅限可信局域网 |
| `CARDMINE_DATA_DIR` | `/data`（Compose 内固定） | 主应用数据目录 |
| `CARDMINE_SCHEDULER` | `1` | `0` 关闭进程内定时抓取，仅本地调试用 |
| `CARDMINE_ANKI_JOB_TIMEOUT` | `1800` | Anki 同步任务超时秒数，超时标记失败 |
| `ANKI_CONNECT_URL` | `http://anki-connect-server:8765/api` | Compose 内固定 |

---

## 8. 已知限制

- 不做复习调度，复习完全由 Anki 负责。
- 只读 RSS，不抓详情页、不补历史；RSS 每次通常只有最新 6 条。
- 不生成答案，背面由人工撰写。
- 网页不支持上传图片；图片只来自 apkg 导入与 RSS 原始媒体。
- 单向推送：Anki/手机端的修改不会回写系统。
- 合并两张卡不会删除 Anki 中右侧那张笔记。
- 主应用无登录鉴权，只应部署在可信局域网。

---

## 9. 安全

- 不要把 `APP_PORT` 暴露到公网；如需外网访问，请自行加反向代理与鉴权。
- `anki-connect-server` 默认映射宿主机 `8765`，且无鉴权；只允许可信局域网访问，不要暴露到公网。
- AnkiWeb 凭据只存在 `.env` 与容器环境中，不写入数据库。
- AI API Key 会明文保存在 SQLite 的 `setting` 表，请保护数据目录与备份文件。

---

## 10. 部署验收清单

逐项确认后再交给日常使用：

- [ ] `docker compose ps` 两个容器均为 `running`
- [ ] `curl http://127.0.0.1:8080/api/health` 返回 `status: ok`
- [ ] 仪表盘可打开，Anki 显示 **已连接**
- [ ] Anki 容器中有预期牌组与笔记（AnkiWeb 同步或 apkg 导入）
- [ ] `/import` 导入 apkg 成功，模块树与卡片数正确
- [ ] 设置页 **接管匹配** 的 `matched` 接近卡片总数
- [ ] 设置页填写 RSS 与 AI 配置后保存
- [ ] 仪表盘 **立即抓取** 成功，无 AI 失败或媒体缺失告警
- [ ] 审核页可处理待确认重复
- [ ] 一张 `ready` 卡片 **立即推送** 后，Anki 中出现对应笔记
- [ ] 设置页 **同步 AnkiWeb** 成功，手机 AnkiDroid 同步后能看到新卡
- [ ] `/export` 可下载 apkg、CSV、SQLite 备份与媒体清单
- [ ] `APP_DATA_PATH/backups/anki/` 出现同步前备份

---

## 11. 常见问题

**Anki 显示未连接**

```bash
docker compose ps
docker compose logs -f anki-connect-server
```

确认容器在运行、容器地址为 `http://anki-connect-server:8765/api`、模型为 `Basic` 且字段为 `Front/Back`。

**同步任务一直停在「备份集合」或 `running`**

新版本已改为按文件复制集合与 `-wal`/`-shm`，不再用 SQLite 在线备份打开 Anki 正在独占的集合。若仍卡住：

1. 查看任务：`curl http://NAS_IP:8080/api/anki/jobs/<job_id>`
2. 取消任务：`curl -X POST http://NAS_IP:8080/api/anki/jobs/<job_id>/cancel`（只把任务标记为失败，已发出的 AnkiConnect 请求可能仍在收尾）
3. 检查 `APP_DATA_PATH/backups/anki/` 是否生成 0 字节文件，有则删除对应备份目录
4. `docker compose restart app` 后重试
5. 若仍报 `A synchronization is already in progress`，再执行 `docker compose restart anki-connect-server` 清掉 Anki 侧残留锁

**直连 AnkiConnect 排障**

默认映射 `ANKI_PORT=8765`，可直接在局域网访问：

```bash
curl -s http://NAS_IP:8765/ \
  -H 'Content-Type: application/json' \
  -d '{"action":"version","version":6}'
```

该端口无鉴权，不要暴露到公网；不需要时可在 `docker-compose.yml` 中注释掉 `ports` 并 `docker compose up -d`。

**全量同步冲突**

保持 `ANKI_FAIL_ON_FULL_SYNC_CONFLICT=true`，先在 Anki 客户端处理冲突；同步前备份位于 `APP_DATA_PATH/backups/anki/`。不要在未备份时关闭保护强行覆盖。

**AI 切题失败**

检查 Base URL、API Key、模型名与向量维度。修复后点击 **立即抓取**，系统会自动重试 `pending/failed` 的文章。

**媒体缺失**

仪表盘会显示缺失数量。重新导入原始 apkg，或确认 `APP_DATA_PATH/media/` 中存在对应文件。网页不支持手动上传图片。

**RSS 没有新内容**

RSS 通常只返回最新 6 条。抓取间隔过长可能漏抓；仪表盘出现「抓取窗口疑似整体滚动」时应缩短间隔。

**数据目录权限错误**

确认 `APP_DATA_PATH`、`ANKI_DATA_PATH` 位于本地磁盘且容器可写；不要使用 SMB/NFS，SQLite WAL 与 Anki 集合锁在网络文件系统上不可靠。

---

## 12. 本地开发（可选）

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r app/requirements.txt

cd app
CARDMINE_DATA_DIR=../data CARDMINE_SCHEDULER=0 \
  ../.venv/bin/uvicorn cardmine.main:app --reload --port 8080
```

浏览器打开 `http://127.0.0.1:8080`。本地开发通常不启动 Anki 容器，仪表盘会显示 Anki 未连接，属正常现象。

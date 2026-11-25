# 多目标对等 Anki 同步 · 实现规格

- 依据：`docs/adr/0007-multi-target-peer-sync.md`
- 状态：待批准
- 日期：2026-11-25

## 1. 范围

把现有单账号 Anki 同步改为按「同步目标」fan-out：目标、组映射、每目标笔记映射/基线/旗帜、整卡冲突、移除与删除传播、目标侧新增归类。包含迁移、后端、前端、部署与验证。不部署、不提交。

## 2. 数据模型

### 2.1 迁移 `app/cardmine/migrations/014_multi_target_sync.sql`

新增：

```sql
anki_target(
  id INTEGER PK, name TEXT NOT NULL, base_url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 0,
  last_sync_at TEXT, last_error TEXT, created_at TEXT NOT NULL
)
group_target(
  group_id INTEGER NOT NULL REFERENCES module(id) ON DELETE CASCADE,
  target_id INTEGER NOT NULL REFERENCES anki_target(id) ON DELETE CASCADE,
  PRIMARY KEY(group_id, target_id)
)
```

- 目标 1：`name='主账号'`，`base_url` 取 `setting['anki_url']`（JSON 解码失败则用 `http://anki-connect-server:8765/api`）；无 setting 也创建。
- `group_target`：现有全部顶层模块（`module.parent_id IS NULL`）映射到目标 1。
- 重建 `anki_sync` 主键 `(kp_id, target_id)`，保留 `anki_note_id / pushed_hash / pushed_at / last_error / pushed_deck / pushed_front_hash / pushed_back_hash / anki_flag / flag_clear_pending`，新增 `detached INTEGER NOT NULL DEFAULT 0`、`last_seen_at TEXT`；现有行 `target_id=1`。索引 `(target_id, anki_note_id)`。
- 新建 `sync_conflict`（整卡）：

```sql
sync_conflict(
  id INTEGER PK, kp_id INTEGER NOT NULL, target_id INTEGER, anki_note_id INTEGER,
  kind TEXT NOT NULL CHECK(kind IN ('edit','delete_edit','duplicate','media')),
  system_front TEXT NOT NULL, system_back TEXT NOT NULL, system_deck TEXT,
  target_front TEXT, target_back TEXT, target_deck TEXT,
  base_front_hash TEXT, base_back_hash TEXT, note TEXT,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved')),
  resolution TEXT CHECK(resolution IN ('system','target')),
  created_at TEXT NOT NULL, resolved_at TEXT
)
CREATE UNIQUE INDEX ux_sync_conflict_pending ON sync_conflict(kp_id) WHERE status='pending';
```

- 旧 `anki_conflict` 逐行迁入 `sync_conflict(kind='edit', target_id=1, resolution: anki→target)`，快照用卡当前字段补全；随后 `DROP TABLE anki_conflict`。
- `DROP INDEX idx_kp_anki_note`；`ALTER TABLE knowledge_point DROP COLUMN anki_note_id`。

### 2.2 领域辅助 `app/cardmine/anki/targets.py`（新增）

- `list_targets(conn, enabled_only=False)`、`get_target`、`create_target`、`update_target`、`delete_target`。
- `target_group_ids`、`set_target_groups`、`group_target_ids`、`card_targets(conn, kp_id)`。
- `scope_paths(conn, target_id)`：映射组路径列表。
- `deck_in_scope(deck, scopes)`、`group_for_deck(conn, target_id, deck)`。
- `client_for(target, guard)`：构造 `AnkiClient(target.base_url)`。
- `target_stats(conn, target_id)`：`groups / ready / linked / pending / conflicts / detached / last_error`。

## 3. 客户端 `app/cardmine/anki/client.py`

- `ensure_model(model_name)`：`modelNames` 不存在时 `createModel`（Front/Back，css 空），再校验字段；供空账号首次同步。
- `sync_web(guard)`：调用 `sync`；`guard=True` 且错误含 "full sync" 时抛 `AnkiError("需要 full sync，已按保护设置中止")`。
- 其余保持不变。

## 4. 同步引擎 `app/cardmine/anki/service.py`（重写）

保留 `normalize_markdown / field_hash / _deck_name / _plain_text` 等纯函数。

### 4.1 入口

- `sync_target(target_id, model_name, sync_enabled, guard, progress, push=True, pull=True)`。
- `push_targets(...)`：遍历启用目标；单个失败记入该目标 `last_error`，不阻塞其他目标。
- `pull_target`、`sync_all_targets`、`takeover_target`、`audit_target`。
- `summary()`：全局计数 + 每目标统计 + 最近推送 + 待处理冲突。

### 4.2 每目标流程

1. `ensure_model`；读取 `scope_paths`。
2. 清除该目标旗帜（`anki_sync` 按 target）。
3. 归档卡：作用域内 `status='archived'` 且有本目标 note → `deleteNotes`，删除本目标 sync 行；失败记 `last_error`。
4. 载入作用域内 `ready/archived` 卡与该目标 sync 行（含 `detached`）。
5. 批量 `notesInfo` 读取已关联 note；`notesInfo.cards` → `cardsInfo` 得到牌组。
6. `pull` 时扫描作用域牌组（根牌组及 `根::*`）的 note，发现未登记 note。
7. 逐卡合并（4.3），逐卡提交；异常写 `anki_sync.last_error` 并继续。
8. 需要时备份（仅目标 URL == `config.ANKI_CONNECT_URL`）并 `sync_web`/`syncMedia`。

### 4.3 三向合并（每卡每目标）

- 基线 = 本目标 `pushed_front_hash / pushed_back_hash`；缺失且 `pushed_hash` 等于当前系统全量哈希时回填。
- 单字段：仅系统变 → push；仅目标变 → 采纳；都变且相同 → 更新基线；都变且不同 → 整卡冲突。
- 目标正面采纳前按组 `front_norm` 精确去重：与组内其他卡重复 → 整卡冲突（`duplicate`）。
- 目标背面采纳前拉取媒体；空背面或媒体失败 → 冲突（`media` 或说明）。
- 冲突：写 `sync_conflict`（系统/目标整卡快照 + 基线哈希），本卡本轮跳过，不改基线。
- 牌组：
  - 目标牌组在本目标映射组的子树内且对应其他模块 → 更新 `knowledge_point.module_id`，组不变。
  - 牌组在子树外 → `detached=1`，保留 note 与基线；不删除。
  - 已 detach 的 note 回到子树 → `detached=0`，继续合并。
- 缺失 note：
  - `detached` → 忽略。
  - 系统自基线后无编辑 → 归档并删除其余目标同一卡 note；删除全部 sync 行。
  - 有编辑 → `delete_edit` 冲突。
- 新 note（未登记）：
  - 仅处理配置模型、正面非空、牌组在本目标映射组子树内。
  - 组内无同正面 → 建 `ready` 卡（`origin='anki'`），登记映射与基线。
  - 同正面同背面 → 绑定已有卡（draft 升 ready），登记映射。
  - 同正面不同背面 → `duplicate` 整卡冲突。
  - 子树外新牌组忽略。
- 媒体推送：目标已存在同名且内容不同 → `media` 冲突，不覆盖；缺失则上传。

### 4.4 冲突

- `list_conflicts(status, limit)` 返回整卡：`system_front/back/deck`、`target_front/back/deck`、目标名、kind、note。
- `resolve_conflict(conflict_id, choice)`，`choice ∈ system|target`：
  - target：把目标整卡写回卡片（含正面去重校验；草稿升 ready）。
  - 关闭冲突；对该卡所有目标置 `anki_sync.force_push=1`（保留基线），下次推送强制把系统整卡分发到所有目标，成功后清除。`undo_conflict` 用同一机制回退。
- `undo_conflict`：仅 target 选择可撤销，恢复系统快照并重开冲突。

### 4.5 接管 / 体检

- `takeover_target`：扫描目标作用域牌组，按 `(组, front_norm)` 匹配未登记卡片，登记映射与基线，不覆盖内容。
- `audit_target`：输出作用域牌组、目标未关联 note、系统有但目标已删除的关联；输出结构保持现有字段名。

## 5. API

### 5.1 `app/cardmine/api/anki.py`

- `GET /api/anki/targets`：目标列表 + 映射组 + 统计。
- `POST /api/anki/targets`、`PATCH /api/anki/targets/{id}`、`DELETE /api/anki/targets/{id}`。
- `PUT /api/anki/targets/{id}/groups`：`{group_ids: []}` 覆盖映射。
- `POST /api/anki/targets/{id}/status` 或 `GET .../status`：连通性、版本、字段。
- 任务：`POST /api/anki/{takeover|audit|push|pull|sync}` 接受可选 `target_id`；缺省遍历启用目标。
- `GET /api/anki/status`：`model / sync_enabled / conflict_guard / targets[] / summary`；保留 `connected/url/version/error`（取第一个启用目标）供仪表盘。
- `GET /api/anki/conflicts`、`POST /conflicts/{id}/resolve`（`system|target`）、`POST /conflicts/{id}/undo`。
- `POST /api/anki/notes/delete`：必须带 `target_id`。
- `GET /api/anki/logs`：按目标过滤（可选 `target_id`）。

### 5.2 其他

- `api/settings.py`：删除 `anki_url` 默认值与入参；保留模型/开关/保护。
- `api/cards.py`：详情增加 `anki_links: [{target_id,target_name,note_id,detached,flag}]`；`conflicts` 读 `sync_conflict`；旗帜清除按 `target_id`；归档 dry-run/统计按 `anki_sync`；合并删除 note 按目标。
- `review.py`：合并时迁移/删除各目标 `anki_sync` 行；不再读写 `kp.anki_note_id`；`unclaimed_notes` 含 `target_id/target_name`；保留 `anki_note_id` 作为首个 note 的兼容字段。
- `api/media.py`：引用扫描改 `sync_conflict(system_front/system_back/target_front/target_back)`。
- `api/stats.py`：`_anki_status` 用第一个启用目标；冲突计数改 `sync_conflict`。
- `db.py`：`_normalize_ai_roots` 合并顶层组时同步重映射 `group_target`（去重）。

## 6. 前端

- `app/web/index.html` + `app.js`
  - 设置表单删除「容器地址」。
  - 同步页：目标列表（名称/URL/启用/连接状态/映射组数/待推送/冲突）、新建/编辑/删除目标、组×目标映射矩阵（复选框）、目标级 推送/拉取/同步/接管/体检按钮与结果。
  - 冲突页：整卡「系统 / 目标」对比（正面、背面、牌组），按钮「用系统」「用目标」「撤销」；解决后自动触发一次全目标推送。
- `app/web/cards.js`：卡片详情/合并弹窗读取 `anki_links`，显示每个目标 note；合并提示按目标去重 note 数量。

## 7. 部署

- `docker-compose.yml`：新增 `anki-connect-friend`（独立容器名/端口 `ANKI_FRIEND_PORT`/卷 `ANKI_FRIEND_DATA_PATH`/凭据 `ANKIWEB_FRIEND_USER/PASS`），两个容器均设 `ANKICONNECT_FAIL_ON_FULL_SYNC_CONFLICT=true`（可用环境变量覆盖）。
- `.env.example` 增加第二个容器的端口、数据路径与凭据项；不写入任何 RSS 地址。

## 8. 验证

1. `python -m compileall app/cardmine`。
2. `python -m unittest discover -s tests -v`：
   - 三向合并：仅系统/仅目标/双方相同/双方不同。
   - 正面采纳与组内重复 → duplicate 冲突。
   - 编辑对删除 → delete_edit。
   - 目标牌组移出子树 → detach；移回 → 恢复。
   - 目标新 note 同正面同背绑定 / 不同背冲突，子树外忽略。
   - 媒体内容不同 → media 冲突。
3. 在 `data/cardmine.db` 副本上跑迁移：`PRAGMA user_version=14`、`anki_target` 1 行、`group_target` 覆盖全部顶层组、`anki_sync` 主键正确、`kp.anki_note_id` 不存在、旧冲突已迁移。
4. 启动本地服务（临时 `CARDMINE_DATA_DIR`），浏览器验证目标管理、映射矩阵、整卡冲突、卡片每目标标记。
5. 完成后不部署、不提交。

## 9. 验收标准

- 迁移可重复在旧库成功执行，无数据丢失。
- 同一前端组可映射两个目标且双向同步；每目标独立失败不影响其他目标。
- 朋友新卡自动进入正确组/模块并去重；移出组只移除分享不删卡；删除双向传播；编辑对删除进冲突。
- 冲突页可按整卡选择并分发到所有目标。
- 仓库不包含私密 RSS 地址。

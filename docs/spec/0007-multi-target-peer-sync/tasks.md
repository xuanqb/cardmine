# 多目标对等同步 · 实施任务

按依赖顺序执行；每项完成后更新任务状态。验证命令见文末。

## A. 数据与基础

- [x] A1 新增 `app/cardmine/migrations/014_multi_target_sync.sql`
  - 建 `anki_target` / `group_target` / `sync_conflict`
  - 迁移 `setting.anki_url` → 目标 1；全部顶层组映射目标 1
  - 重建 `anki_sync(kp_id,target_id)` + `detached/last_seen_at`
  - 迁移 `anki_conflict` 后删除；删除 `idx_kp_anki_note` 与 `kp.anki_note_id`
- [x] A2 新增 `app/cardmine/anki/targets.py`
  - CRUD、组映射、`scope_paths`、`deck_in_scope`、`group_for_deck`、`client_for`、`target_stats`
- [x] A3 `app/cardmine/anki/client.py`
  - `ensure_model`（空目标 `createModel`）、`sync_web(guard)`（full sync 保护）
- [x] A4 `app/cardmine/db.py`
  - `_normalize_ai_roots` 重映射 `group_target`（去重）

## B. 同步引擎

- [x] B1 重写 `app/cardmine/anki/service.py`
  - `sync_target / push_targets / pull_target / sync_all_targets`
  - 每目标推送/拉取/归档删除/旗帜/媒体上传
  - 三向合并、正面组内去重、整卡冲突、detach/恢复、删除传播
  - 目标侧新增 note 发现与归类、子树外忽略
  - `takeover_target / audit_target`
  - `list_conflicts / resolve_conflict / undo_conflict / summary`
- [x] B2 `app/cardmine/collect/service.py`
  - 自动推送改为 `push_targets(settings, progress)`，结果按目标汇总

## C. 接口与调用方

- [x] C1 `app/cardmine/api/anki.py`
  - 目标 CRUD、组映射、目标状态、按目标任务、整卡冲突、按目标 logs/notes delete、新 status 结构
- [x] C2 `app/cardmine/api/settings.py`
  - 删除 `anki_url` 默认值与入参
- [x] C3 `app/cardmine/api/cards.py`
  - `anki_links`、`sync_conflict`、按目标旗帜、归档统计、合并删除 note
- [x] C4 `app/cardmine/review.py`
  - 合并/撤销按目标迁移 `anki_sync`，移除 `kp.anki_note_id`
- [x] C5 `app/cardmine/api/media.py`、`api/stats.py`
  - 新冲突表与首目标状态
- [x] C6 `app/cardmine/api/stats.py` / 仪表盘兼容
  - `anki` 字段取第一个启用目标

## D. 前端

- [x] D1 `app/web/index.html`、`app/web/app.js`
  - 同步目标列表/新建/编辑/删除、组×目标映射矩阵、目标级任务按钮
  - 整卡冲突展示与「用系统/用目标/撤销」，解决后触发全目标推送
  - 删除「容器地址」输入
- [x] D2 `app/web/cards.js`、`app/web/card.js`
  - 详情/合并弹窗按 `anki_links` 展示每目标 note；冲突展示整卡

## E. 部署与验证

- [x] E1 `docker-compose.yml`、`.env.example`
  - 第二个 AnkiConnect 容器、独立卷与凭据、full sync 失败保护
- [x] E2 测试 `tests/test_anki_sync.py`、`tests/test_migration_014.py`
  - 合并决策、正面冲突、编辑对删除、detach/恢复、新卡去重、媒体冲突、迁移
- [x] E3 验证
  - `python -m compileall app/cardmine`
  - `python -m unittest discover -s tests -v`
  - 旧库副本迁移检查
  - 本地启动 + 浏览器验证目标/映射/冲突/卡片标记

## 验证命令

```bash
python -m compileall app/cardmine
python -m unittest discover -s tests -v
```

```bash
tmp=$(mktemp -d); cp data/cardmine.db "$tmp/cardmine.db"
CARDMINE_DATA_DIR="$tmp" python -c "from cardmine.db import init_db; init_db()"
sqlite3 "$tmp/cardmine.db" "PRAGMA user_version; SELECT * FROM anki_target; SELECT COUNT(*) FROM group_target; PRAGMA table_info(anki_sync); PRAGMA table_info(knowledge_point);"
```

不部署、不提交；只有用户明确要求时才 commit/push。

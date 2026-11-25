-- 多目标对等同步：同步目标、组映射、每目标笔记映射、整卡冲突。

CREATE TABLE anki_target (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT NOT NULL,
  base_url    TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1,
  sort        INTEGER NOT NULL DEFAULT 0,
  last_sync_at TEXT,
  last_error  TEXT,
  created_at  TEXT NOT NULL
);

-- 现有容器地址成为第一个同步目标；没有设置时使用部署默认地址。
INSERT INTO anki_target(name, base_url, enabled, sort, created_at)
SELECT
  '主账号',
  COALESCE(
    NULLIF(
      CASE
        WHEN json_valid(value) THEN json_extract(value, '$')
        ELSE trim(value, '"')
      END,
      ''
    ),
    'http://anki-connect-server:8765/api'
  ),
  1,
  0,
  strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
FROM setting WHERE key = 'anki_url';

INSERT INTO anki_target(name, base_url, enabled, sort, created_at)
SELECT '主账号', 'http://anki-connect-server:8765/api', 1, 0,
       strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
WHERE NOT EXISTS (SELECT 1 FROM anki_target);

CREATE TABLE group_target (
  group_id  INTEGER NOT NULL REFERENCES module(id) ON DELETE CASCADE,
  target_id INTEGER NOT NULL REFERENCES anki_target(id) ON DELETE CASCADE,
  PRIMARY KEY (group_id, target_id)
);

-- 现有顶层组全部归入第一个目标；新组由用户在页面另行映射。
INSERT INTO group_target(group_id, target_id)
SELECT id, 1 FROM module WHERE parent_id IS NULL;

-- 每个目标独立维护笔记映射与字段基线。
CREATE TABLE anki_sync_new (
  kp_id               INTEGER NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
  target_id           INTEGER NOT NULL REFERENCES anki_target(id) ON DELETE CASCADE,
  anki_note_id        INTEGER,
  pushed_hash         TEXT,
  pushed_at           TEXT,
  last_error          TEXT,
  pushed_deck         TEXT,
  pushed_front_hash   TEXT,
  pushed_back_hash    TEXT,
  anki_flag           INTEGER NOT NULL DEFAULT 0,
  flag_clear_pending  INTEGER NOT NULL DEFAULT 0,
  detached            INTEGER NOT NULL DEFAULT 0,
  force_push          INTEGER NOT NULL DEFAULT 0,
  last_seen_at        TEXT,
  PRIMARY KEY (kp_id, target_id)
);
INSERT INTO anki_sync_new
  (kp_id, target_id, anki_note_id, pushed_hash, pushed_at, last_error,
   pushed_deck, pushed_front_hash, pushed_back_hash, anki_flag,
   flag_clear_pending)
SELECT kp_id, 1, anki_note_id, pushed_hash, pushed_at, last_error,
       pushed_deck, pushed_front_hash, pushed_back_hash, anki_flag,
       flag_clear_pending
FROM anki_sync;
DROP TABLE anki_sync;
ALTER TABLE anki_sync_new RENAME TO anki_sync;
CREATE INDEX idx_anki_sync_target_note ON anki_sync(target_id, anki_note_id);

-- 冲突改为整卡：一侧的整张卡片与另一侧整张卡片比较。
CREATE TABLE sync_conflict (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  kp_id            INTEGER NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
  target_id        INTEGER REFERENCES anki_target(id) ON DELETE SET NULL,
  anki_note_id     INTEGER,
  kind             TEXT NOT NULL
                   CHECK (kind IN ('edit', 'delete_edit', 'duplicate', 'media')),
  system_front     TEXT NOT NULL,
  system_back      TEXT NOT NULL,
  system_deck      TEXT,
  target_front     TEXT,
  target_back      TEXT,
  target_deck      TEXT,
  base_front_hash  TEXT,
  base_back_hash   TEXT,
  note             TEXT,
  status           TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'resolved')),
  resolution       TEXT CHECK (resolution IN ('system', 'target')),
  created_at       TEXT NOT NULL,
  resolved_at      TEXT
);
CREATE UNIQUE INDEX ux_sync_conflict_pending
  ON sync_conflict(kp_id) WHERE status = 'pending';
CREATE INDEX idx_sync_conflict_status ON sync_conflict(status, id);

-- 旧字段级冲突保留快照，收敛为整卡冲突。
INSERT INTO sync_conflict
  (kp_id, target_id, anki_note_id, kind, system_front, system_back,
   system_deck, target_front, target_back, target_deck, note, status,
   resolution, created_at, resolved_at)
SELECT
  c.kp_id,
  1,
  c.anki_note_id,
  'edit',
  CASE WHEN c.field = 'front' THEN c.system_value ELSE COALESCE(kp.front_md, '') END,
  CASE WHEN c.field = 'back' THEN c.system_value ELSE COALESCE(kp.back_md, '') END,
  COALESCE(replace(m.path, '/', '::'), 'Default'),
  CASE WHEN c.field = 'front' THEN c.anki_value ELSE COALESCE(kp.front_md, '') END,
  CASE WHEN c.field = 'back' THEN c.anki_value ELSE COALESCE(kp.back_md, '') END,
  COALESCE(replace(m.path, '/', '::'), 'Default'),
  c.note,
  CASE WHEN c.status = 'pending' THEN 'pending' ELSE 'resolved' END,
  CASE
    WHEN c.resolution = 'anki' THEN 'target'
    WHEN c.resolution = 'system' THEN 'system'
    ELSE NULL
  END,
  c.created_at,
  c.resolved_at
FROM anki_conflict c
JOIN knowledge_point kp ON kp.id = c.kp_id
LEFT JOIN module m ON m.id = kp.module_id;
DROP TABLE anki_conflict;

-- 每目标映射成为权威来源。
DROP INDEX IF EXISTS idx_kp_anki_note;
ALTER TABLE knowledge_point DROP COLUMN anki_note_id;

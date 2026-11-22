-- 合并来源正面别名：来源卡删除后仍参与后续去重匹配
CREATE TABLE IF NOT EXISTS kp_alias (
  id              INTEGER PRIMARY KEY,
  owner_kp_id     INTEGER NOT NULL,
  front_md        TEXT NOT NULL,
  front_norm      TEXT NOT NULL UNIQUE,
  module_id       INTEGER REFERENCES module(id) ON DELETE SET NULL,
  merge_action_id INTEGER,
  embed_dim       INTEGER,
  embedding       BLOB,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kp_alias_owner ON kp_alias(owner_kp_id);

-- 来源卡 -> 存活卡的合并链，用于别名归属追踪
CREATE TABLE IF NOT EXISTS kp_merge_edge (
  source_kp_id   INTEGER PRIMARY KEY,
  survivor_kp_id INTEGER NOT NULL,
  action_id      INTEGER NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kp_merge_edge_survivor
  ON kp_merge_edge(survivor_kp_id);

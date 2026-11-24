-- 组（Group）= 顶层模块。来源、卡片、别名都归属一个组，去重与匹配按组隔离。

ALTER TABLE feed ADD COLUMN group_id INTEGER REFERENCES module(id) ON DELETE SET NULL;
ALTER TABLE knowledge_point ADD COLUMN group_id INTEGER REFERENCES module(id) ON DELETE SET NULL;

-- 重建 kp_alias：去掉 front_norm 的全局 UNIQUE，改为按组唯一。
CREATE TABLE kp_alias_new (
  id              INTEGER PRIMARY KEY,
  owner_kp_id     INTEGER NOT NULL,
  front_md        TEXT NOT NULL,
  front_norm      TEXT NOT NULL,
  module_id       INTEGER REFERENCES module(id) ON DELETE SET NULL,
  group_id        INTEGER REFERENCES module(id) ON DELETE SET NULL,
  merge_action_id INTEGER,
  embed_dim       INTEGER,
  embedding       BLOB,
  created_at      TEXT NOT NULL
);
INSERT INTO kp_alias_new
  (id, owner_kp_id, front_md, front_norm, module_id, merge_action_id,
   embed_dim, embedding, created_at)
SELECT id, owner_kp_id, front_md, front_norm, module_id, merge_action_id,
       embed_dim, embedding, created_at
FROM kp_alias;
DROP TABLE kp_alias;
ALTER TABLE kp_alias_new RENAME TO kp_alias;
CREATE INDEX idx_kp_alias_owner ON kp_alias(owner_kp_id);

-- 旧卡片按所在模块的顶层模块归组；没有模块的卡片保持未分组（NULL）。
CREATE TEMP TABLE _module_root AS
WITH RECURSIVE roots(id, root) AS (
  SELECT id, id FROM module WHERE parent_id IS NULL
  UNION ALL
  SELECT m.id, roots.root FROM module m JOIN roots ON m.parent_id = roots.id
)
SELECT id, root FROM roots;

UPDATE knowledge_point
SET group_id = (
  SELECT root FROM _module_root WHERE _module_root.id = knowledge_point.module_id
)
WHERE module_id IS NOT NULL;

UPDATE kp_alias
SET group_id = COALESCE(
  (SELECT group_id FROM knowledge_point kp WHERE kp.id = kp_alias.owner_kp_id),
  (SELECT root FROM _module_root WHERE _module_root.id = kp_alias.module_id)
);
DROP TABLE _module_root;

-- 旧的 RSS 源归入现有卡片最多的组；没有卡片时保持未分组。
UPDATE feed
SET group_id = (
  SELECT group_id FROM knowledge_point
  WHERE group_id IS NOT NULL
  GROUP BY group_id
  ORDER BY COUNT(*) DESC, group_id
  LIMIT 1
)
WHERE group_id IS NULL;

DROP INDEX IF EXISTS ux_kp_front_norm;
CREATE UNIQUE INDEX ux_kp_group_front
  ON knowledge_point(group_id, front_norm) WHERE group_id IS NOT NULL;
CREATE UNIQUE INDEX ux_kp_null_front
  ON knowledge_point(front_norm) WHERE group_id IS NULL;
CREATE UNIQUE INDEX ux_kp_alias_group_front
  ON kp_alias(group_id, front_norm) WHERE group_id IS NOT NULL;
CREATE UNIQUE INDEX ux_kp_alias_null_front
  ON kp_alias(front_norm) WHERE group_id IS NULL;

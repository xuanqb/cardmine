-- 「保持独立」是否失效只看正面：记录两张卡当时的 front_norm。
-- 背面内容变化（AI 答案、Anki 拉取、重新导入）不再触发重新比对。
CREATE TABLE dup_exclusion_new (
  left_kp_id       INTEGER NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
  right_kp_id      INTEGER NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
  left_front_norm  TEXT,
  right_front_norm TEXT,
  action_id        INTEGER REFERENCES review_action(id) ON DELETE SET NULL,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (left_kp_id, right_kp_id)
);

INSERT INTO dup_exclusion_new
  (left_kp_id, right_kp_id, left_front_norm, right_front_norm, action_id, created_at)
SELECT e.left_kp_id, e.right_kp_id, lk.front_norm, rk.front_norm,
       e.action_id, e.created_at
FROM dup_exclusion e
JOIN knowledge_point lk ON lk.id = e.left_kp_id
JOIN knowledge_point rk ON rk.id = e.right_kp_id;

DROP TABLE dup_exclusion;
ALTER TABLE dup_exclusion_new RENAME TO dup_exclusion;
CREATE INDEX IF NOT EXISTS idx_dup_exclusion_action
  ON dup_exclusion(action_id);

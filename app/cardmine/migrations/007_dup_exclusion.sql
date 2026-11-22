-- 保持独立后的排除记录：内容未变化时不再重复生成候选
CREATE TABLE IF NOT EXISTS dup_exclusion (
  left_kp_id  INTEGER NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
  right_kp_id INTEGER NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
  left_hash   TEXT NOT NULL,
  right_hash  TEXT NOT NULL,
  action_id   INTEGER REFERENCES review_action(id) ON DELETE SET NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (left_kp_id, right_kp_id)
);
CREATE INDEX IF NOT EXISTS idx_dup_exclusion_action
  ON dup_exclusion(action_id);

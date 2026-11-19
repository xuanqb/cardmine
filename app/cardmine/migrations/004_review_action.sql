-- 审核操作留痕，支持撤销
CREATE TABLE IF NOT EXISTS review_action (
  id           INTEGER PRIMARY KEY,
  candidate_id INTEGER REFERENCES dup_candidate(id) ON DELETE SET NULL,
  action       TEXT NOT NULL,
  payload      TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  undone_at    TEXT
);

CREATE TABLE ai_thread (
  id          INTEGER PRIMARY KEY,
  kp_id       INTEGER NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
  title       TEXT NOT NULL DEFAULT '',
  front_norm  TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE INDEX idx_ai_thread_kp ON ai_thread(kp_id, updated_at DESC);

CREATE TABLE ai_message (
  id          INTEGER PRIMARY KEY,
  thread_id   INTEGER NOT NULL REFERENCES ai_thread(id) ON DELETE CASCADE,
  role        TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
  content     TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'done',
  created_at  TEXT NOT NULL
);

CREATE INDEX idx_ai_message_thread ON ai_message(thread_id, id);

ALTER TABLE knowledge_point
  ADD COLUMN ai_answer_message_id INTEGER REFERENCES ai_message(id) ON DELETE SET NULL;

-- Anki 背面双向同步：字段级基线与冲突记录
ALTER TABLE anki_sync ADD COLUMN pushed_front_hash TEXT;
ALTER TABLE anki_sync ADD COLUMN pushed_back_hash TEXT;

CREATE TABLE IF NOT EXISTS anki_conflict (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kp_id         INTEGER NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
  anki_note_id  INTEGER NOT NULL,
  field         TEXT NOT NULL CHECK (field IN ('front', 'back')),
  base_hash     TEXT,
  system_value  TEXT NOT NULL,
  anki_value    TEXT NOT NULL,
  system_hash   TEXT NOT NULL,
  anki_hash     TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'resolved')),
  resolution    TEXT CHECK (resolution IN ('system', 'anki')),
  note          TEXT,
  created_at    TEXT NOT NULL,
  resolved_at   TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_anki_conflict_pending
  ON anki_conflict(kp_id, field) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_anki_conflict_status
  ON anki_conflict(status, id);

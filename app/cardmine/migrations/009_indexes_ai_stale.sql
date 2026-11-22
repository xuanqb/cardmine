-- 补查询索引；记录 AI 答案生成时对应的正面，用于判断答案是否过时
ALTER TABLE knowledge_point ADD COLUMN ai_front_norm TEXT;

CREATE INDEX IF NOT EXISTS idx_mention_kp ON mention(kp_id);
CREATE INDEX IF NOT EXISTS idx_kp_module ON knowledge_point(module_id);
CREATE INDEX IF NOT EXISTS idx_kp_status ON knowledge_point(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_kp_anki_note ON knowledge_point(anki_note_id);

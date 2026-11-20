ALTER TABLE knowledge_point ADD COLUMN ai_back_md TEXT;
ALTER TABLE knowledge_point ADD COLUMN ai_status TEXT NOT NULL DEFAULT 'none';
ALTER TABLE knowledge_point ADD COLUMN ai_error TEXT;
ALTER TABLE knowledge_point ADD COLUMN ai_generated_at TEXT;

-- 向量存储兜底：运行环境不支持加载 sqlite-vec 扩展时使用
CREATE TABLE IF NOT EXISTS kp_embedding (
  kp_id  INTEGER PRIMARY KEY REFERENCES knowledge_point(id) ON DELETE CASCADE,
  dim    INTEGER NOT NULL,
  vector BLOB NOT NULL
);

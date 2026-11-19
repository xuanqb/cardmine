CREATE TABLE feed (
  id                INTEGER PRIMARY KEY,
  url               TEXT NOT NULL UNIQUE,
  name              TEXT,
  enabled           INTEGER NOT NULL DEFAULT 1,
  interval_minutes  INTEGER NOT NULL DEFAULT 60,
  last_fetched_at   TEXT
);

CREATE TABLE post (
  id          INTEGER PRIMARY KEY,
  guid        TEXT NOT NULL UNIQUE,
  feed_id     INTEGER NOT NULL REFERENCES feed(id),
  title       TEXT NOT NULL,
  author      TEXT,
  url         TEXT,
  raw_html    TEXT,
  clean_md    TEXT,
  fetched_at  TEXT NOT NULL,
  ai_status   TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE mention (
  id          INTEGER PRIMARY KEY,
  post_id     INTEGER NOT NULL REFERENCES post(id) ON DELETE CASCADE,
  seq         INTEGER NOT NULL,
  raw_text    TEXT NOT NULL,
  context_md  TEXT,
  section     TEXT,
  topic       TEXT NOT NULL,
  subtype     TEXT,
  kp_id       INTEGER REFERENCES knowledge_point(id) ON DELETE SET NULL,
  status      TEXT NOT NULL DEFAULT 'pending',
  created_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX ux_mention_post_text ON mention(post_id, raw_text);

CREATE TABLE module (
  id         INTEGER PRIMARY KEY,
  parent_id  INTEGER REFERENCES module(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  path       TEXT NOT NULL UNIQUE,
  sort       INTEGER NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT 'manual'
);

CREATE TABLE knowledge_point (
  id            INTEGER PRIMARY KEY,
  front_md      TEXT NOT NULL,
  back_md       TEXT NOT NULL DEFAULT '',
  front_norm    TEXT NOT NULL,
  module_id     INTEGER REFERENCES module(id) ON DELETE SET NULL,
  status        TEXT NOT NULL DEFAULT 'draft',
  origin        TEXT NOT NULL DEFAULT 'crawl',
  anki_note_id  INTEGER,
  content_hash  TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE UNIQUE INDEX ux_kp_front_norm ON knowledge_point(front_norm);

CREATE TABLE dup_candidate (
  id          INTEGER PRIMARY KEY,
  left_kp_id  INTEGER REFERENCES knowledge_point(id) ON DELETE CASCADE,
  right_kp_id INTEGER REFERENCES knowledge_point(id) ON DELETE CASCADE,
  mention_id  INTEGER REFERENCES mention(id) ON DELETE CASCADE,
  score       REAL NOT NULL,
  reason      TEXT,
  status      TEXT NOT NULL DEFAULT 'pending',
  created_at  TEXT NOT NULL,
  resolved_at TEXT
);

CREATE TABLE anki_sync (
  kp_id        INTEGER PRIMARY KEY REFERENCES knowledge_point(id) ON DELETE CASCADE,
  anki_note_id INTEGER,
  pushed_hash  TEXT,
  pushed_at    TEXT,
  last_error   TEXT
);

CREATE TABLE media (
  id         INTEGER PRIMARY KEY,
  filename   TEXT NOT NULL UNIQUE,
  sha256     TEXT NOT NULL,
  size       INTEGER NOT NULL,
  mime       TEXT,
  path       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE fetch_log (
  id          INTEGER PRIMARY KEY,
  feed_id     INTEGER NOT NULL REFERENCES feed(id),
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  items       INTEGER DEFAULT 0,
  new_posts   INTEGER DEFAULT 0,
  all_new     INTEGER DEFAULT 0,
  error       TEXT
);

CREATE TABLE setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

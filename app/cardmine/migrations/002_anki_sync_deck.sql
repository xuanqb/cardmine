-- 记录上次推送所在牌组，用于检测模块变更后 changeDeck
ALTER TABLE anki_sync ADD COLUMN pushed_deck TEXT;

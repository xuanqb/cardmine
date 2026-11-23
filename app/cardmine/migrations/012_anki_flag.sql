-- Anki 旗帜只读同步。anki_flag 保存最近一次拉取到的颜色（0-7）；
-- flag_clear_pending 保存待清除时看到的颜色，0 表示没有待清除。
ALTER TABLE anki_sync ADD COLUMN anki_flag INTEGER NOT NULL DEFAULT 0;
ALTER TABLE anki_sync ADD COLUMN flag_clear_pending INTEGER NOT NULL DEFAULT 0;

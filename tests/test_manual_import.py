import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from cardmine import config, db  # noqa: E402
from cardmine.collect.service import (  # noqa: E402
    import_manual_post,
    preview_manual_post,
)

SETTINGS = {
    "ai_base_url": "",
    "ai_api_key": "",
    "ai_chat_model": "",
    "ai_embedding_model": "",
    "ai_embedding_base_url": "",
    "ai_embedding_api_key": "",
    "dup_high_threshold": 0.9,
    "dup_low_threshold": 0.8,
    "ai_embedding_dim": 4,
}

AI_SETTINGS = {
    **SETTINGS,
    "ai_base_url": "http://127.0.0.1:1/v1",
    "ai_api_key": "test",
    "ai_chat_model": "test",
}


class ManualImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data_dir = Path(self.tmp.name)
        self._saved = (
            config.DATA_DIR,
            config.DB_PATH,
            config.MEDIA_DIR,
            config.BACKUP_DIR,
        )
        config.DATA_DIR = data_dir
        config.DB_PATH = data_dir / "cardmine.db"
        config.MEDIA_DIR = data_dir / "media"
        config.BACKUP_DIR = data_dir / "backups"
        self.addCleanup(self._restore_config)
        db.init_db()
        with closing(db.connect()) as conn, conn:
            self.group_id = conn.execute(
                "INSERT INTO module(parent_id, name, path) "
                "VALUES(NULL, 'Backend', 'Backend')"
            ).lastrowid

    def _restore_config(self):
        (
            config.DATA_DIR,
            config.DB_PATH,
            config.MEDIA_DIR,
            config.BACKUP_DIR,
        ) = self._saved

    def mention(self, question, module=None):
        return {
            "question": question,
            "topic": "tech",
            "subtype": None,
            "section": "一面",
            "context": "",
            "module": module or "Backend/MySQL",
        }

    def test_import_counts_mentions_and_attaches(self):
        first = import_manual_post(
            SETTINGS,
            "字节一面",
            "整篇面经文字",
            self.group_id,
            [self.mention("MySQL 索引为什么用 B+ 树")],
        )
        self.assertEqual(first["mentions"], 1)
        self.assertEqual(first["created"], 1)

        second = import_manual_post(
            SETTINGS,
            "字节二面",
            "另一整篇面经文字",
            self.group_id,
            [self.mention("MySQL 索引为什么用 B+ 树")],
        )
        self.assertEqual(second["mentions"], 1)
        self.assertEqual(second["attached"], 1)

        with closing(db.connect()) as conn:
            cards = conn.execute(
                "SELECT id FROM knowledge_point WHERE group_id = ?",
                (self.group_id,),
            ).fetchall()
            self.assertEqual(len(cards), 1)
            mentions = conn.execute(
                "SELECT COUNT(*) AS value FROM mention WHERE kp_id = ?",
                (cards[0]["id"],),
            ).fetchone()["value"]
            self.assertEqual(mentions, 2)

            posts = conn.execute(
                "SELECT title, clean_md, ai_status FROM post ORDER BY id"
            ).fetchall()
            self.assertEqual([row["title"] for row in posts], ["字节一面", "字节二面"])
            self.assertTrue(all(row["ai_status"] == "done" for row in posts))
            feed = conn.execute(
                "SELECT url, enabled, group_id FROM feed"
            ).fetchone()
            self.assertEqual(feed["url"], f"manual://group/{self.group_id}")
            self.assertEqual(feed["enabled"], 0)

    def test_duplicate_question_in_one_import_is_deduped(self):
        result = import_manual_post(
            SETTINGS,
            None,
            "text",
            self.group_id,
            [self.mention("Redis 持久化"), self.mention("Redis 持久化")],
        )
        self.assertEqual(result["mentions"], 1)

    def test_title_defaults_to_first_line(self):
        import_manual_post(
            SETTINGS,
            None,
            "\n# 2024 秋招 字节一面\n\n面试正文",
            self.group_id,
            [self.mention("第一个问题")],
        )
        import_manual_post(
            SETTINGS,
            "手动标题",
            "正文",
            self.group_id,
            [self.mention("第二个问题")],
        )
        with closing(db.connect()) as conn:
            titles = [
                row["title"]
                for row in conn.execute("SELECT title FROM post ORDER BY id")
            ]
        self.assertEqual(titles, ["2024 秋招 字节一面", "手动标题"])

    @mock.patch(
        "cardmine.collect.service.extract_mentions",
        return_value=[{"question": "q", "topic": "tech"}],
    )
    def test_preview_uses_first_line_as_title(self, extract):
        preview_manual_post(AI_SETTINGS, None, "第一行标题\n正文", self.group_id)
        self.assertEqual(extract.call_args.args[1]["title"], "第一行标题")

    def test_requires_group_and_text(self):
        with self.assertRaises(ValueError):
            import_manual_post(SETTINGS, None, "text", 999, [self.mention("q")])
        with self.assertRaises(ValueError):
            import_manual_post(SETTINGS, None, "   ", self.group_id, [self.mention("q")])
        with self.assertRaises(ValueError):
            import_manual_post(SETTINGS, None, "text", self.group_id, [])

    @mock.patch(
        "cardmine.collect.service.extract_mentions",
        return_value=[{"question": "q", "topic": "tech"}],
    )
    def test_preview_does_not_write(self, extract):
        result = preview_manual_post(AI_SETTINGS, "标题", "正文", self.group_id)
        self.assertEqual(result["mentions"], [{"question": "q", "topic": "tech"}])
        self.assertEqual(extract.call_args.args[3], "Backend")
        with closing(db.connect()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS value FROM post").fetchone()["value"],
                0,
            )

    def test_preview_requires_ai(self):
        with self.assertRaises(ValueError):
            preview_manual_post(SETTINGS, None, "正文", self.group_id)

    def test_preview_rejects_overlong_text(self):
        with self.assertRaises(ValueError):
            preview_manual_post(AI_SETTINGS, None, "x" * 16001, self.group_id)


if __name__ == "__main__":
    unittest.main()

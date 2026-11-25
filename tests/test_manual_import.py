import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from cardmine import config, db  # noqa: E402
from cardmine.ai import AIError  # noqa: E402
from cardmine.collect.service import delete_manual_post, run_manual_post  # noqa: E402

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


def mention(question, module="Backend/MySQL"):
    return {
        "question": question,
        "topic": "tech",
        "subtype": None,
        "section": "一面",
        "context": "",
        "module": module,
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

    def run_import(self, title, text, mentions, settings=None):
        with mock.patch(
            "cardmine.collect.service.extract_mentions", return_value=mentions
        ):
            return run_manual_post(
                settings or AI_SETTINGS, title, text, self.group_id
            )

    def test_import_counts_mentions_and_attaches(self):
        question = "MySQL 索引为什么用 B+ 树"
        first = self.run_import("字节一面", "整篇面经文字", [mention(question)])
        self.assertEqual(first["mentions"], 1)
        self.assertEqual(first["created"], 1)
        self.assertEqual(first["items"][0]["action"], "created")

        second = self.run_import("字节二面", "另一整篇面经文字", [mention(question)])
        self.assertEqual(second["mentions"], 1)
        self.assertEqual(second["attached"], 1)
        self.assertEqual(second["items"][0]["action"], "attached")
        self.assertEqual(second["items"][0]["kp_front"], question)

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

    def test_delete_post_removes_its_draft_cards(self):
        result = self.run_import(
            "标题", "正文", [mention("第一个问题"), mention("第二个问题")]
        )
        deleted = delete_manual_post(result["post_id"])
        self.assertEqual(deleted["removed_cards"], 2)

        with closing(db.connect()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS value FROM post").fetchone()["value"],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) AS value FROM mention").fetchone()["value"],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS value FROM knowledge_point"
                ).fetchone()["value"],
                0,
            )

    def test_delete_keeps_answered_cards(self):
        result = self.run_import("标题", "正文", [mention("第一个问题")])
        with closing(db.connect()) as conn, conn:
            conn.execute(
                "UPDATE knowledge_point SET status = 'ready', back_md = '答案'"
            )
        deleted = delete_manual_post(result["post_id"])
        self.assertEqual(deleted["removed_cards"], 0)
        with closing(db.connect()) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) AS value FROM knowledge_point"
                ).fetchone()["value"],
                1,
            )

    def test_delete_rejects_regular_feed_post(self):
        with closing(db.connect()) as conn, conn:
            feed_id = conn.execute(
                "INSERT INTO feed(url, name, enabled, interval_minutes, group_id) "
                "VALUES('https://example.com/rss.xml', 'RSS', 1, 60, ?)",
                (self.group_id,),
            ).lastrowid
            post_id = conn.execute(
                "INSERT INTO post(guid, feed_id, title, fetched_at) "
                "VALUES('g1', ?, 'RSS 面经', '2026-11-25T00:00:00+00:00')",
                (feed_id,),
            ).lastrowid
        with self.assertRaises(ValueError):
            delete_manual_post(post_id)

    def test_manual_feed_hidden_from_fetch_status(self):
        self.run_import("标题", "正文", [mention("第一个问题")])
        with closing(db.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO feed(url, name, enabled, interval_minutes) "
                "VALUES('https://example.com/rss.xml', 'RSS', 1, 60)"
            )
        from cardmine.api.stats import _fetch_status

        with closing(db.connect()) as conn:
            feeds = _fetch_status(conn)["feeds"]
        self.assertEqual([feed["url"] for feed in feeds], ["https://example.com/rss.xml"])

    def test_title_defaults_to_first_line(self):
        self.run_import(None, "\n# 2024 秋招 字节一面\n\n面试正文", [mention("第一个问题")])
        self.run_import("手动标题", "正文", [mention("第二个问题")])
        with closing(db.connect()) as conn:
            titles = [
                row["title"]
                for row in conn.execute("SELECT title FROM post ORDER BY id")
            ]
        self.assertEqual(titles, ["2024 秋招 字节一面", "手动标题"])

    def test_requires_group_text_and_ai(self):
        with self.assertRaises(ValueError):
            run_manual_post(AI_SETTINGS, None, "text", 999)
        with self.assertRaises(ValueError):
            run_manual_post(AI_SETTINGS, None, "   ", self.group_id)
        with self.assertRaises(ValueError):
            run_manual_post(SETTINGS, None, "text", self.group_id)

    def test_rejects_overlong_text(self):
        with self.assertRaises(ValueError):
            run_manual_post(AI_SETTINGS, None, "x" * 16001, self.group_id)

    def test_ai_failure_marks_post_failed(self):
        with mock.patch(
            "cardmine.collect.service.extract_mentions",
            side_effect=AIError("boom"),
        ):
            result = run_manual_post(AI_SETTINGS, None, "text", self.group_id)
        self.assertEqual(result["ai_failed"], 1)
        self.assertEqual(result["mentions"], 0)
        self.assertEqual(result["items"], [])
        self.assertIn("boom", result["errors"][0])
        with closing(db.connect()) as conn:
            status = conn.execute("SELECT ai_status FROM post").fetchone()["ai_status"]
        self.assertEqual(status, "failed")

    def test_progress_reports_phases(self):
        phases = []
        with mock.patch(
            "cardmine.collect.service.extract_mentions",
            return_value=[mention("第一个问题")],
        ):
            run_manual_post(
                AI_SETTINGS,
                None,
                "text",
                self.group_id,
                lambda phase, done, total: phases.append(phase),
            )
        self.assertIn("补全向量", phases)
        self.assertIn("AI 切题", phases)
        self.assertIn("归并题目", phases)


if __name__ == "__main__":
    unittest.main()

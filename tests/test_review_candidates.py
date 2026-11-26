import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from cardmine import config, db, review  # noqa: E402

TS = "2026-11-26T00:00:00+00:00"


class SamePostFlagTest(unittest.TestCase):
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

    def _restore_config(self):
        (
            config.DATA_DIR,
            config.DB_PATH,
            config.MEDIA_DIR,
            config.BACKUP_DIR,
        ) = self._saved

    def _seed(self, conn, title: str, questions: list[str]) -> dict[str, int]:
        row = conn.execute(
            "SELECT id FROM module WHERE parent_id IS NULL LIMIT 1"
        ).fetchone()
        group_id = (
            row["id"]
            if row is not None
            else conn.execute(
                "INSERT INTO module(parent_id, name, path) VALUES(NULL, 'G', 'G')"
            ).lastrowid
        )
        feed_id = conn.execute(
            "INSERT INTO feed(url, name, enabled, interval_minutes, group_id) "
            "VALUES(?, 'F', 0, 60, ?)",
            (f"manual://group/{group_id}/{title}", group_id),
        ).lastrowid
        post_id = conn.execute(
            "INSERT INTO post(guid, feed_id, title, fetched_at) VALUES(?, ?, ?, ?)",
            (title, feed_id, title, TS),
        ).lastrowid
        kps = {}
        for seq, question in enumerate(questions, 1):
            kp_id = conn.execute(
                "INSERT INTO knowledge_point"
                "(front_md, front_norm, group_id, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (question, question, group_id, TS, TS),
            ).lastrowid
            conn.execute(
                "INSERT INTO mention(post_id, seq, raw_text, topic, kp_id, status, created_at) "
                "VALUES(?, ?, ?, 'tech', ?, 'attached', ?)",
                (post_id, seq, question, kp_id, TS),
            )
            kps[question] = kp_id
        return kps

    def test_flags_only_pairs_sharing_a_post(self):
        with closing(db.connect()) as conn, conn:
            same = self._seed(conn, "同一篇", ["左侧", "右侧"])
            other = self._seed(conn, "另一篇", ["右外"])
            conn.execute(
                "INSERT INTO dup_candidate(left_kp_id, right_kp_id, score, created_at) "
                "VALUES(?, ?, 0.85, ?)",
                (same["左侧"], same["右侧"], "2026-11-26T00:00:01+00:00"),
            )
            conn.execute(
                "INSERT INTO dup_candidate(left_kp_id, right_kp_id, score, created_at) "
                "VALUES(?, ?, 0.85, ?)",
                (same["左侧"], other["右外"], "2026-11-26T00:00:02+00:00"),
            )

        data = review.list_candidates("pending", 1, 10)
        flags = {
            (item["left_kp_id"], item["right_kp_id"]): item["same_post"]
            for item in data["items"]
        }
        self.assertEqual(flags[(same["左侧"], same["右侧"])], 1)
        self.assertEqual(flags[(same["左侧"], other["右外"])], 0)


if __name__ == "__main__":
    unittest.main()

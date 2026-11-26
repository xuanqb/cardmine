import struct
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from cardmine import config, db  # noqa: E402
from cardmine.api.cards import CardUpdate, update_card  # noqa: E402

TS = "2026-11-26T00:00:00+00:00"


class FrontVectorTest(unittest.TestCase):
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
            group_id = conn.execute(
                "INSERT INTO module(parent_id, name, path) VALUES(NULL, 'G', 'G')"
            ).lastrowid
            self.kp_id = conn.execute(
                "INSERT INTO knowledge_point"
                "(front_md, front_norm, group_id, created_at, updated_at) "
                "VALUES('原正面', '原正面', ?, ?, ?)",
                (group_id, TS, TS),
            ).lastrowid
            conn.execute(
                "INSERT INTO kp_embedding(kp_id, dim, vector) VALUES(?, 4, ?)",
                (self.kp_id, struct.pack("4f", 0.1, 0.2, 0.3, 0.4)),
            )

    def _restore_config(self):
        (
            config.DATA_DIR,
            config.DB_PATH,
            config.MEDIA_DIR,
            config.BACKUP_DIR,
        ) = self._saved

    def _embedding_rows(self) -> int:
        with closing(db.connect()) as conn:
            return conn.execute(
                "SELECT COUNT(*) AS value FROM kp_embedding WHERE kp_id = ?",
                (self.kp_id,),
            ).fetchone()["value"]

    def test_front_change_drops_vectors(self):
        update_card(self.kp_id, CardUpdate(front_md="新正面"))
        self.assertEqual(self._embedding_rows(), 0)
        with closing(db.connect()) as conn:
            row = conn.execute(
                "SELECT front_md, front_norm FROM knowledge_point WHERE id = ?",
                (self.kp_id,),
            ).fetchone()
        self.assertEqual(row["front_md"], "新正面")
        self.assertEqual(row["front_norm"], "新正面")

    def test_back_change_keeps_vectors(self):
        update_card(self.kp_id, CardUpdate(back_md="新背面"))
        self.assertEqual(self._embedding_rows(), 1)

    def test_unchanged_front_keeps_vectors(self):
        update_card(self.kp_id, CardUpdate(front_md="原正面 ", back_md="新背面"))
        self.assertEqual(self._embedding_rows(), 1)


if __name__ == "__main__":
    unittest.main()

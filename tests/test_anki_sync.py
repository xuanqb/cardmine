import json
import base64
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from cardmine import config, db  # noqa: E402
from cardmine.anki import service, targets  # noqa: E402
from cardmine.anki.client import AnkiClient, AnkiError  # noqa: E402
from cardmine.importer.textfmt import normalize_front  # noqa: E402


class ModelClient(AnkiClient):
    """在内存中模拟 Anki 模型与样式的客户端。"""

    def __init__(self, models=None):
        super().__init__("http://fake")
        self.models = models or {}
        self.media = {}
        self.calls = []
        self.synced = 0

    def sync_web(self, timeout=600):
        self.synced += 1

    def call(self, action, timeout=60, **params):
        self.calls.append((action, params))
        if action == "version":
            return 6
        if action == "modelNames":
            return sorted(self.models)
        if action == "modelStyling":
            return {"css": self.models[params["modelName"]]["css"]}
        if action == "modelTemplates":
            return self.models[params["modelName"]]["templates"]
        if action == "modelFieldNames":
            return ["Front", "Back"]
        if action == "getMediaFilesNames":
            return sorted(self.media)
        if action == "retrieveMediaFile":
            return self.media.get(params["filename"], False)
        if action == "storeMediaFile":
            self.media[params["filename"]] = params["data"]
            return None
        if action == "createModel":
            self.models[params["modelName"]] = {
                "css": params["css"],
                "templates": {
                    item["Name"]: {"Front": item["Front"], "Back": item["Back"]}
                    for item in params.get("cardTemplates", [])
                },
            }
            return None
        if action == "updateModelStyling":
            self.models[params["model"]["name"]]["css"] = params["model"]["css"]
            return None
        if action == "updateModelTemplates":
            self.models[params["model"]["name"]]["templates"] = params["model"][
                "templates"
            ]
            return None
        if action == "modelTemplateAdd":
            templates = self.models[params["modelName"]]["templates"]
            template = params["template"]
            for other_name, other in templates.items():
                if (
                    other_name != template["Name"]
                    and (other.get("Front") or "").strip()
                    == template["Front"].strip()
                ):
                    raise AnkiError(
                        f"Card template {template['Name']} in note type "
                        f"'{params['modelName']}' has a problem. "
                        f"The front side is identical to card template {other_name}."
                    )
            templates[template["Name"]] = {
                "Front": template["Front"],
                "Back": template["Back"],
            }
            return None
        if action == "modelTemplateRemove":
            self.models[params["modelName"]]["templates"].pop(
                params["templateName"], None
            )
            return None
        if action == "modelTemplateRename":
            templates = self.models[params["modelName"]]["templates"]
            old_name = params["oldTemplateName"]
            new_name = params["newTemplateName"]
            if new_name in templates:
                raise AnkiError(f"template already exists: {new_name}")
            templates[new_name] = templates.pop(old_name)
            return None
        raise AssertionError(f"未预期的动作：{action}")


class FakeClient:
    """最小 AnkiConnect 替身，覆盖同步流程用到的动作。"""

    def __init__(self):
        self.notes = {}
        self.deck_notes = {}
        self.decks = ["Backend"]
        self.calls = []
        self.next_note = 900

    def ensure_model(self, name, timeout=60, source=None):
        self.calls.append(("ensure_model", {"name": name, "source": source}))

    def sync_web(self):
        self.calls.append(("sync", {}))

    def add_note(self, front, back, deck="Backend", note_id=None):
        note_id = note_id or self.next_note
        self.next_note = max(self.next_note + 1, note_id + 1)
        self.notes[note_id] = {
            "noteId": note_id,
            "modelName": "Basic",
            "cards": [note_id * 10],
            "deck": deck,
            "fields": {"Front": {"value": front}, "Back": {"value": back}},
        }
        self.deck_notes.setdefault(deck, set()).add(note_id)
        return note_id

    def set_deck(self, note_id, deck):
        self.notes[note_id]["deck"] = deck
        for ids in self.deck_notes.values():
            ids.discard(note_id)
        self.deck_notes.setdefault(deck, set()).add(note_id)
        if deck not in self.decks:
            self.decks.append(deck)

    def actions(self, name):
        return [params for action, params in self.calls if action == name]

    def call(self, action, **params):
        self.calls.append((action, params))
        if action == "deckNames":
            return list(self.decks)
        if action == "notesInfo":
            return [
                self.notes[note]
                for note in params.get("notes", [])
                if note in self.notes
            ]
        if action == "cardsInfo":
            result = []
            for note in self.notes.values():
                if note["cards"] and note["cards"][0] in params.get("cards", []):
                    result.append(
                        {"cardId": note["cards"][0], "deckName": note["deck"]}
                    )
            return result
        if action == "findNotes":
            query = params.get("query", "")
            if query.startswith('deck:"') and query.endswith('"'):
                deck = query[6:-1]
                return sorted(self.deck_notes.get(deck, set()))
            return []
        if action == "findCards":
            query = params.get("query", "")
            if query.startswith("nid:"):
                note = self.notes.get(int(query[4:]))
                return list(note["cards"]) if note else []
            return []
        if action == "cardsToNotes":
            return []
        if action == "updateNoteFields":
            note = self.notes[int(params["note"]["id"])]
            for key, value in params["note"]["fields"].items():
                note["fields"][key]["value"] = value
            return None
        if action == "addNote":
            payload = params["note"]
            return self.add_note(
                payload["fields"]["Front"],
                payload["fields"]["Back"],
                payload["deckName"],
            )
        if action == "createDeck":
            if params["deck"] not in self.decks:
                self.decks.append(params["deck"])
            return None
        if action == "changeDeck":
            for note in self.notes.values():
                if note["cards"] and note["cards"][0] in params["cards"]:
                    self.set_deck(note["noteId"], params["deck"])
            return None
        if action == "deleteNotes":
            for note_id in list(self.notes):
                note = self.notes[note_id]
                if note["cards"] and note["cards"][0] in params["notes"]:
                    del self.notes[note_id]
                    self.deck_notes.get(note["deck"], set()).discard(note_id)
            return None
        raise AssertionError(f"未预期的动作：{action}")


class SyncTestCase(unittest.TestCase):
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
            cursor = conn.execute(
                "INSERT INTO module(parent_id, name, path) VALUES(NULL, 'Backend', 'Backend')"
            )
            self.group_id = cursor.lastrowid
            conn.execute(
                "INSERT INTO group_target(group_id, target_id) VALUES(?, 1)",
                (self.group_id,),
            )
        self.target_id = 1

    def _restore_config(self):
        (
            config.DATA_DIR,
            config.DB_PATH,
            config.MEDIA_DIR,
            config.BACKUP_DIR,
        ) = self._saved

    def make_card(self, front, back, status="ready", module_id=None):
        with closing(db.connect()) as conn, conn:
            cursor = conn.execute(
                """
                INSERT INTO knowledge_point
                  (front_md, back_md, front_norm, module_id, group_id, status,
                   origin, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, 'crawl', '2026-01-01T00:00:00+00:00',
                       '2026-01-01T00:00:00+00:00')
                """,
                (
                    front,
                    back,
                    normalize_front(front),
                    module_id or self.group_id,
                    self.group_id,
                    status,
                ),
            )
            return cursor.lastrowid

    def bind(
        self,
        kp_id,
        note_id,
        front,
        back,
        deck="Backend",
        combined=None,
    ):
        with closing(db.connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO anki_sync
                  (kp_id, target_id, anki_note_id, pushed_hash, pushed_at,
                   pushed_deck, pushed_front_hash, pushed_back_hash)
                VALUES(?, ?, ?, ?, '2026-01-01T00:00:00+00:00', ?, ?, ?)
                """,
                (
                    kp_id,
                    self.target_id,
                    note_id,
                    combined,
                    deck,
                    service.field_hash(front),
                    service.field_hash(back),
                ),
            )

    def run_sync(self, client, push=True, pull=True):
        with mock.patch.object(targets, "client_for", return_value=client):
            return service.sync_target(
                self.target_id,
                "Basic",
                sync_enabled=False,
                guard=True,
                push=push,
                pull=pull,
            )

    def scalar(self, sql, params=()):
        with closing(db.connect()) as conn:
            row = conn.execute(sql, params).fetchone()
            return row[0] if row else None

    def card_front(self, kp_id):
        return self.scalar(
            "SELECT front_md FROM knowledge_point WHERE id = ?", (kp_id,)
        )

    def sync_front(self, kp_id):
        return self.scalar(
            "SELECT pushed_front_hash FROM anki_sync "
            "WHERE kp_id = ? AND target_id = ?",
            (kp_id, self.target_id),
        )

    def conflicts(self):
        with closing(db.connect()) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM sync_conflict ORDER BY id"
                )
            ]


class FieldActionTest(unittest.TestCase):
    def test_field_action(self):
        base = service.field_hash("base")
        changed = service.field_hash("changed")
        other = service.field_hash("other")

        self.assertEqual(service.field_action(base, base, base), "none")
        self.assertEqual(service.field_action(changed, base, base), "push")
        self.assertEqual(service.field_action(base, changed, base), "pull")
        self.assertEqual(service.field_action(changed, changed, base), "same")
        self.assertEqual(service.field_action(changed, other, base), "conflict")
        self.assertEqual(
            service.field_action(changed, changed, None), "same"
        )
        self.assertEqual(
            service.field_action(changed, other, None), "conflict"
        )


class ThreeWayMergeTest(SyncTestCase):
    def _case(self, system_front, target_front, expect):
        kp_id = self.make_card(system_front, "back")
        self.bind(kp_id, 10, "base", "back")
        client = FakeClient()
        client.add_note(target_front, "back", note_id=10)
        result = self.run_sync(client)
        return kp_id, client, result

    def test_system_only_changed_pushes(self):
        kp_id, client, result = self._case("system", "base", "push")
        self.assertEqual(client.actions("updateNoteFields")[0]["note"]["fields"][
            "Front"
        ], "system")
        self.assertEqual(result["updated"], 1)
        self.assertEqual(
            client.notes[10]["fields"]["Front"]["value"], "system"
        )
        self.assertEqual(self.sync_front(kp_id), service.field_hash("system"))
        self.assertEqual(self.conflicts(), [])

    def test_target_only_changed_pulls(self):
        kp_id, client, result = self._case("base", "target", "pull")
        self.assertEqual(self.card_front(kp_id), "target")
        self.assertEqual(result["pulled"], 1)
        self.assertEqual(client.actions("updateNoteFields"), [])

    def test_both_changed_same_updates_baseline(self):
        kp_id, client, result = self._case("same", "same", "same")
        self.assertEqual(self.card_front(kp_id), "same")
        self.assertEqual(client.actions("updateNoteFields"), [])
        self.assertEqual(self.sync_front(kp_id), service.field_hash("same"))
        self.assertEqual(result["conflicts"], 0)

    def test_both_changed_differ_records_conflict(self):
        kp_id, client, result = self._case("system", "target", "conflict")
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(client.actions("updateNoteFields"), [])
        rows = self.conflicts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "edit")
        self.assertEqual(rows[0]["system_front"], "system")
        self.assertEqual(rows[0]["target_front"], "target")


class ImportAndDeleteTest(SyncTestCase):
    def test_new_note_binds_same_front_and_back(self):
        kp_id = self.make_card("existing", "back")
        client = FakeClient()
        client.add_note("existing", "back", note_id=55)
        result = self.run_sync(client)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(
            self.scalar(
                "SELECT anki_note_id FROM anki_sync "
                "WHERE kp_id = ? AND target_id = ?",
                (kp_id, self.target_id),
            ),
            55,
        )
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM knowledge_point"), 1
        )

    def test_new_note_same_front_different_back_conflicts(self):
        self.make_card("same", "system")
        client = FakeClient()
        client.add_note("same", "friend", note_id=56)
        result = self.run_sync(client)
        self.assertEqual(result["conflicts"], 1)
        rows = self.conflicts()
        self.assertEqual(rows[0]["kind"], "duplicate")
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM knowledge_point"), 1
        )

    def test_note_outside_group_is_ignored(self):
        client = FakeClient()
        client.add_note("outside", "back", note_id=57, deck="Other")
        self.run_sync(client)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM knowledge_point"), 0
        )

    def test_missing_note_without_edit_archives(self):
        kp_id = self.make_card("card", "back")
        self.bind(kp_id, 10, "card", "back")
        client = FakeClient()
        result = self.run_sync(client)
        self.assertEqual(result["changed"], 1)
        self.assertEqual(
            self.scalar("SELECT status FROM knowledge_point WHERE id = ?", (kp_id,)),
            "archived",
        )
        self.assertIsNone(
            self.scalar(
                "SELECT anki_note_id FROM anki_sync WHERE kp_id = ?", (kp_id,)
            )
        )

    def test_missing_note_with_edit_records_delete_edit(self):
        kp_id = self.make_card("edited", "back")
        self.bind(kp_id, 10, "card", "back")
        client = FakeClient()
        result = self.run_sync(client)
        self.assertEqual(result["conflicts"], 1)
        rows = self.conflicts()
        self.assertEqual(rows[0]["kind"], "delete_edit")
        self.assertEqual(
            self.scalar("SELECT status FROM knowledge_point WHERE id = ?", (kp_id,)),
            "ready",
        )


class DetachTest(SyncTestCase):
    def test_move_out_detaches_and_move_back_restores(self):
        kp_id = self.make_card("card", "back")
        self.bind(kp_id, 10, "card", "back")
        client = FakeClient()
        client.add_note("card", "back", note_id=10, deck="Outside")

        first = self.run_sync(client)
        self.assertEqual(first["detached"], 1)
        self.assertEqual(
            self.scalar(
                "SELECT detached FROM anki_sync WHERE kp_id = ? AND target_id = ?",
                (kp_id, self.target_id),
            ),
            1,
        )
        self.assertEqual(
            self.scalar("SELECT status FROM knowledge_point WHERE id = ?", (kp_id,)),
            "ready",
        )

        client.set_deck(10, "Backend/Sub")
        second = self.run_sync(client)
        self.assertEqual(second["detached"], 0)
        self.assertEqual(
            self.scalar(
                "SELECT detached FROM anki_sync WHERE kp_id = ? AND target_id = ?",
                (kp_id, self.target_id),
            ),
            0,
        )


class MediaConflictTest(SyncTestCase):
    def test_media_content_diff_records_conflict(self):
        kp_id = self.make_card("front", "old ![img](pic.png)")
        self.bind(kp_id, 10, "front", "old")
        client = FakeClient()
        client.add_note("front", "old", note_id=10)
        with closing(db.connect()) as conn, conn:
            row = service._load_rows(conn, self.target_id, ("ready",))[0]
            result = service._merge_existing(
                conn,
                client,
                "Basic",
                self.target_id,
                row,
                client.notes[10],
                "Backend",
                {"Backend"},
                "push",
                {"pic.png"},
            )
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(client.actions("updateNoteFields"), [])
        rows = self.conflicts()
        self.assertEqual(rows[0]["kind"], "media")
        self.assertIn("pic.png", rows[0]["note"])

    def test_media_same_content_pushes_normally(self):
        kp_id = self.make_card("front", "new")
        self.bind(kp_id, 10, "front", "old")
        client = FakeClient()
        client.add_note("front", "old", note_id=10)
        with closing(db.connect()) as conn, conn:
            row = service._load_rows(conn, self.target_id, ("ready",))[0]
            result = service._merge_existing(
                conn,
                client,
                "Basic",
                self.target_id,
                row,
                client.notes[10],
                "Backend",
                {"Backend"},
                "push",
                set(),
            )
        self.assertEqual(result["conflicts"], 0)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(client.notes[10]["fields"]["Back"]["value"], "new")


class ModelStyleTest(SyncTestCase):
    CSS = ".card { font-family: 'Custom', sans-serif; }"
    TEMPLATES = {
        "Card 1": {
            "Front": "{{Front}}",
            "Back": "{{FrontSide}}\n<hr id=answer>\n{{Back}}",
        },
        "Reverse": {
            "Front": "{{Back}}",
            "Back": "{{FrontSide}}\n<hr id=answer>\n{{Front}}",
        },
    }

    def setUp(self):
        super().setUp()
        with closing(db.connect()) as conn, conn:
            self.friend_id = conn.execute(
                "INSERT INTO anki_target(name, base_url, enabled, sort, created_at) "
                "VALUES('朋友', 'http://friend:8765/api', 1, 1, "
                "'2026-01-01T00:00:00+00:00')"
            ).lastrowid

    def _clients(self, target_models, source_templates=None):
        self.source = ModelClient(
            {
                "Basic": {
                    "css": self.CSS,
                    "templates": source_templates or self.TEMPLATES,
                }
            }
        )
        self.target = ModelClient(target_models)
        clients = {self.target_id: self.source, self.friend_id: self.target}
        return mock.patch.object(
            targets,
            "client_for",
            side_effect=lambda row, guard=True: clients[int(row["id"])],
        )

    def test_ensure_model_copies_style_when_creating(self):
        source = ModelClient(
            {"Basic": {"css": self.CSS, "templates": self.TEMPLATES}}
        )
        target = ModelClient({})
        target.ensure_model("Basic", source=source)
        self.assertEqual(target.models["Basic"]["css"], self.CSS)
        self.assertEqual(target.models["Basic"]["templates"], self.TEMPLATES)
        created = [params for action, params in target.calls if action == "createModel"]
        self.assertEqual(len(created[0]["cardTemplates"]), 2)

    def test_ensure_model_without_source_uses_default_css(self):
        target = ModelClient({})
        target.ensure_model("Basic")
        self.assertIn(".card", target.models["Basic"]["css"])
        self.assertEqual(target.models["Basic"]["templates"], {})

    def test_sync_style_updates_existing_model(self):
        with self._clients(
            {"Basic": {"css": ".card { color: black; }", "templates": {}}}
        ):
            result = service.sync_model_style(self.friend_id, "Basic")
        self.assertEqual(result["source_name"], "主账号")
        self.assertEqual(result["templates"], 2)
        self.assertEqual(self.target.models["Basic"]["css"], self.CSS)
        self.assertEqual(self.target.models["Basic"]["templates"], self.TEMPLATES)

    def test_sync_style_creates_missing_model(self):
        with self._clients({}):
            result = service.sync_model_style(self.friend_id, "Basic")
        self.assertEqual(result["css"], True)
        self.assertEqual(self.target.models["Basic"]["css"], self.CSS)
        self.assertEqual(self.target.models["Basic"]["templates"], self.TEMPLATES)

    def test_sync_style_adds_missing_template_by_name(self):
        target_models = {
            "Basic": {
                "css": ".card { color: black; }",
                "templates": {
                    "Card 1": {"Front": "{{Front}}", "Back": "{{FrontSide}}"}
                },
            }
        }
        with self._clients(target_models):
            result = service.sync_model_style(self.friend_id, "Basic")
        self.assertEqual(result["templates"], 2)
        self.assertEqual(self.target.models["Basic"]["templates"], self.TEMPLATES)
        actions = [action for action, _ in self.target.calls]
        self.assertIn("modelTemplateAdd", actions)
        self.assertNotIn("updateModelTemplates", actions)

    def test_sync_style_repairs_duplicate_templates(self):
        target_models = {
            "Basic": {
                "css": ".card { color: black; }",
                "templates": {
                    "Card 1": {
                        "Front": "{{Back}}",
                        "Back": "{{FrontSide}}",
                    },
                    "Card 2": {
                        "Front": "{{Back}}",
                        "Back": "{{FrontSide}}",
                    },
                },
            }
        }
        with self._clients(target_models):
            service.sync_model_style(self.friend_id, "Basic")
        self.assertEqual(self.target.models["Basic"]["templates"], self.TEMPLATES)

    def test_sync_style_removes_extra_template(self):
        source_templates = {"Card 1": self.TEMPLATES["Card 1"]}
        target_models = {
            "Basic": {
                "css": ".card { color: black; }",
                "templates": {
                    "Card 1": {"Front": "{{Front}}", "Back": "{{FrontSide}}"},
                    "Card 2": {"Front": "{{Front}}", "Back": "{{FrontSide}}"},
                },
            }
        }
        with self._clients(target_models, source_templates=source_templates):
            service.sync_model_style(self.friend_id, "Basic")
        self.assertEqual(
            self.target.models["Basic"]["templates"], source_templates
        )

    def test_sync_style_renames_sole_template(self):
        source_templates = {"Card 1": self.TEMPLATES["Card 1"]}
        target_models = {
            "Basic": {
                "css": ".card { color: black; }",
                "templates": {
                    "Front": {"Front": "{{Front}}", "Back": "{{FrontSide}}"}
                },
            }
        }
        with self._clients(target_models, source_templates=source_templates):
            service.sync_model_style(self.friend_id, "Basic")
        self.assertEqual(
            self.target.models["Basic"]["templates"], source_templates
        )

    def test_sync_style_renames_templates_when_names_differ(self):
        target_models = {
            "Basic": {
                "css": ".card { color: black; }",
                "templates": {
                    "正面": {"Front": "{{Front}}", "Back": "{{FrontSide}}"},
                    "背面": {"Front": "{{Back}}", "Back": "{{FrontSide}}"},
                },
            }
        }
        with self._clients(target_models):
            service.sync_model_style(self.friend_id, "Basic")
        self.assertEqual(self.target.models["Basic"]["templates"], self.TEMPLATES)

    def test_sync_style_rejects_duplicate_source(self):
        duplicate = {
            "Card 1": {"Front": "{{Front}}", "Back": ""},
            "Card 2": {"Front": "{{Front}}", "Back": ""},
        }
        with self._clients({}, source_templates=duplicate):
            with self.assertRaises(AnkiError):
                service.sync_model_style(self.friend_id, "Basic")

    def test_sync_style_copies_template_media(self):
        templates = {
            "Card 1": {
                "Front": "{{Front}}",
                "Back": '{{FrontSide}}<script src="markdown-it.min.js"></script>',
            }
        }
        payload = base64.b64encode(b"console.log(1)").decode("ascii")
        with self._clients({}, source_templates=templates):
            self.source.media["markdown-it.min.js"] = payload
            result = service.sync_model_style(self.friend_id, "Basic")
        self.assertEqual(result["media_copied"], 1)
        self.assertEqual(self.target.media["markdown-it.min.js"], payload)
        self.assertEqual(result["media_errors"], [])

    def test_sync_style_skips_existing_media(self):
        templates = {
            "Card 1": {
                "Front": "{{Front}}",
                "Back": '{{FrontSide}}<script src="markdown-it.min.js"></script>',
            }
        }
        with self._clients({}, source_templates=templates):
            self.source.media["markdown-it.min.js"] = "c291cmNl"
            self.target.media["markdown-it.min.js"] = "dGFyZ2V0"
            result = service.sync_model_style(self.friend_id, "Basic")
        self.assertEqual(result["media_copied"], 0)
        self.assertEqual(self.target.media["markdown-it.min.js"], "dGFyZ2V0")

    def test_sync_style_pushes_to_ankiweb_when_enabled(self):
        with self._clients({}):
            result = service.sync_model_style(
                self.friend_id, "Basic", sync_enabled=True
            )
        self.assertEqual(result["sync"], "ok")
        self.assertEqual(self.target.synced, 1)

    def test_sync_style_rejects_same_source(self):
        with self._clients({}):
            with self.assertRaises(ValueError):
                service.sync_model_style(
                    self.friend_id, "Basic", source_target_id=self.friend_id
                )


class _MigrationBase(unittest.TestCase):
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

    def _restore_config(self):
        (
            config.DATA_DIR,
            config.DB_PATH,
            config.MEDIA_DIR,
            config.BACKUP_DIR,
        ) = self._saved

    def apply_until(self, version):
        conn = db.connect()
        conn.execute("PRAGMA foreign_keys = OFF")
        for path in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
            number = int(path.name.split("_", 1)[0])
            if number > version:
                break
            conn.executescript(
                f"BEGIN;\n{path.read_text(encoding='utf-8')}\n"
                f"PRAGMA user_version = {number};\nCOMMIT;"
            )
        return conn


class Migration014Test(_MigrationBase):
    def test_migrates_old_database(self):
        conn = self.apply_until(13)
        with conn:
            conn.execute(
                "INSERT INTO setting(key, value) VALUES('anki_url', ?)",
                (json.dumps("http://custom-anki:8765/api"),),
            )
            cursor = conn.execute(
                "INSERT INTO module(parent_id, name, path) VALUES(NULL, 'Backend', 'Backend')"
            )
            group_id = cursor.lastrowid
            kp = conn.execute(
                """
                INSERT INTO knowledge_point
                  (front_md, back_md, front_norm, module_id, group_id, status,
                   origin, created_at, updated_at)
                VALUES('Q', 'A', 'q', ?, ?, 'ready', 'crawl',
                       '2026-01-01T00:00:00+00:00',
                       '2026-01-01T00:00:00+00:00')
                """,
                (group_id, group_id),
            ).lastrowid
            conn.execute(
                "INSERT INTO anki_sync(kp_id, anki_note_id, pushed_front_hash, "
                "pushed_back_hash) VALUES(?, 42, 'f', 'b')",
                (kp,),
            )
            conn.execute(
                """
                INSERT INTO anki_conflict
                  (kp_id, anki_note_id, field, system_value, anki_value,
                   system_hash, anki_hash, status, note, created_at)
                VALUES(?, 42, 'back', 'sys', 'anki', 's', 'a', 'pending',
                       '冲突', '2026-01-01T00:00:00+00:00')
                """,
                (kp,),
            )
        conn.close()

        db.init_db()

        with closing(db.connect()) as check:
            self.assertEqual(
                check.execute("PRAGMA user_version").fetchone()[0], 14
            )
            target = check.execute("SELECT * FROM anki_target").fetchall()
            self.assertEqual(len(target), 1)
            self.assertEqual(target[0]["base_url"], "http://custom-anki:8765/api")
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM group_target").fetchone()[0],
                1,
            )
            self.assertEqual(
                [row[1] for row in check.execute("PRAGMA table_info(anki_sync)")],
                [
                    "kp_id",
                    "target_id",
                    "anki_note_id",
                    "pushed_hash",
                    "pushed_at",
                    "last_error",
                    "pushed_deck",
                    "pushed_front_hash",
                    "pushed_back_hash",
                    "anki_flag",
                    "flag_clear_pending",
                    "detached",
                    "force_push",
                    "last_seen_at",
                ],
            )
            columns = {
                row[1]
                for row in check.execute("PRAGMA table_info(knowledge_point)")
            }
            self.assertNotIn("anki_note_id", columns)
            self.assertEqual(
                check.execute("SELECT COUNT(*) FROM anki_conflict").fetchone()[0]
                if check.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='anki_conflict'"
                ).fetchone()
                else 0,
                0,
            )
            conflict = check.execute("SELECT * FROM sync_conflict").fetchall()
            self.assertEqual(len(conflict), 1)
            self.assertEqual(conflict[0]["kind"], "edit")
            self.assertEqual(conflict[0]["target_id"], 1)
            self.assertEqual(conflict[0]["status"], "pending")

    def test_migration_is_idempotent(self):
        db.init_db()
        db.init_db()
        with closing(db.connect()) as conn:
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 14
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM anki_target").fetchone()[0], 1
            )


if __name__ == "__main__":
    unittest.main()

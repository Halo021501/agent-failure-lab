import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from lab.common import ROOT, LabError, NotFound, canonical, content_hash
from lab.runner import Runner
from lab.storage import Store


class RevisionCacheTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "data/verification").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data/verification")
        self.store = Store(self.temp.name)
        self.runner = Runner(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def completed(self):
        eid = self.runner.create_experiment(["N01"])
        page = self.runner.execute_experiment(eid)
        self.store.experiment_page(eid)
        return eid, page["runs"][0]

    def test_other_experiment_writes_preserve_run_and_overview_cache(self):
        eid, run = self.completed()
        other = self.runner.create_experiment(["N02"])
        other_id = self.store.experiment_info(other)["runs"][0]["id"]
        self.store.event(other_id, "probe", {"value":1})
        with patch.object(self.store, "events", side_effect=AssertionError("trace reloaded")):
            self.assertTrue(self.store.run(run["id"])["evaluation"]["contract_pass"])
        with patch.object(self.store, "run", side_effect=AssertionError("overview reloaded")):
            self.assertEqual(self.store.experiment_page(eid)["summary"]["contract_pass"], 1)

    def test_own_connection_event_delete_and_artifact_update_invalidate(self):
        for sql in ("DELETE FROM events WHERE run_id=? AND seq=1", "UPDATE artifacts SET sha='changed' WHERE run_id=?"):
            eid, run = self.completed()
            with self.store.lock, self.store.db:
                self.store.db.execute(sql, (run["id"],))
            page = self.store.experiment_page(eid)
            self.assertEqual(page["summary"]["contract_pass"], 0)
            self.assertIsNone(page["runs"][0]["evaluation"])

    def test_evaluation_delete_restores_original_and_transaction_rollback(self):
        eid, run = self.completed()
        revision = self.store._revision("run", run["id"])
        with self.assertRaises(RuntimeError):
            with self.store.lock, self.store.db:
                self.store._event(run["id"], "rollback", {})
                raise RuntimeError("rollback")
        self.assertEqual(self.store._revision("run", run["id"]), revision)
        changed = deepcopy(run["evaluation"])
        changed.update(contract_pass=False, complete_success=False)
        ev_id = self.store.add_evaluation(run["id"], changed, "local-test")
        self.assertEqual(self.store.experiment_page(eid)["summary"]["contract_pass"], 0)
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM evaluations WHERE id=?", (ev_id,))
        self.assertEqual(self.store.experiment_page(eid)["summary"]["contract_pass"], 1)

    def test_moved_event_invalidates_old_and_new_experiments(self):
        left, first = self.completed()
        right, second = self.completed()
        before_left = self.store._revision("experiment", left)
        before_right = self.store._revision("experiment", right)
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE events SET run_id=?,seq=99999 WHERE run_id=? AND seq=1", (second["id"], first["id"]))
        self.assertNotEqual(before_left, self.store._revision("experiment", left))
        self.assertNotEqual(before_right, self.store._revision("experiment", right))
        self.assertEqual(self.store.experiment_page(left)["summary"]["contract_pass"], 0)
        self.assertEqual(self.store.experiment_page(right)["summary"]["contract_pass"], 0)

    def test_v2_upgrade_backs_up_database_and_report_without_changing_trace(self):
        eid, run = self.completed()
        before_events = self.store.events(run["id"])
        report_path = Path(self.temp.name) / "runs" / run["id"] / "report.json"
        original_report = report_path.read_bytes()
        with self.store.lock, self.store.db:
            for row in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                self.store.db.execute('DROP TRIGGER "' + row[0] + '"')
            self.store.db.execute("DROP TABLE cache_revisions")
            self.store.db.execute("PRAGMA user_version=2")
        self.store.close()
        self.store = Store(self.temp.name, recover=False)
        backup = Path(self.store.migration_backup)
        with closing(sqlite3.connect(backup / "lab.db")) as old:
            self.assertEqual(old.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertFalse(old.execute("SELECT 1 FROM sqlite_master WHERE name='cache_revisions'").fetchone())
        self.assertEqual((backup / "runs" / run["id"] / "report.json").read_bytes(), original_report)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.store.events(run["id"]), before_events)
        self.assertEqual(self.store.experiment_page(eid)["summary"]["contract_pass"], 1)

    def test_initialization_failure_rolls_back_and_closes_connection(self):
        directory = Path(self.temp.name) / "broken-migration"
        with patch("lab.storage.revision_triggers", return_value="INVALID SQL;"):
            with self.assertRaises(sqlite3.OperationalError):
                Store(directory)
        with closing(sqlite3.connect(directory / "lab.db")) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
        recovered = Store(directory)
        recovered.close()


class PayloadCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data/verification")
        self.store = Store(self.temp.name)
        self.runner = Runner(self.store)
        eid = self.runner.create_experiment(["N01"])
        self.rid = self.store.experiment_info(eid)["runs"][0]["id"]
        self.payload = {"text":"资料🙂" * 5000}
        self.seq = self.store.event(self.rid, "probe", self.payload)
        self.sha = content_hash(canonical(self.payload))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_preview_populates_cache_and_chunks_skip_full_parse_and_hash(self):
        preview = self.store.events(self.rid, preview=True)[0]
        self.assertEqual(preview["payload_sha256"], self.sha)
        with patch("lab.storage.json.loads", side_effect=AssertionError("parsed again")), \
             patch("lab.storage.content_hash", side_effect=AssertionError("hashed again")):
            part = self.store.event_payload(self.rid, self.seq, self.sha, offset=4096)
        self.assertEqual(part["text"], canonical(self.payload)[4096:8192])
        self.assertEqual(self.store.payload_cache_hits, 1)

    def test_other_run_write_keeps_payload_but_same_run_update_and_delete_invalidate(self):
        self.store.event_payload(self.rid, self.seq, self.sha)
        other = self.runner.create_experiment(["N02"])
        rid = self.store.experiment_info(other)["runs"][0]["id"]
        self.store.event(rid, "probe", {})
        with patch("lab.storage.content_hash", side_effect=AssertionError("hashed again")):
            self.store.event_payload(self.rid, self.seq, self.sha)
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE events SET payload='{}' WHERE run_id=? AND seq=?", (self.rid, self.seq))
        with self.assertRaises(LabError) as changed:
            self.store.event_payload(self.rid, self.seq, self.sha)
        self.assertEqual(changed.exception.code, "payload_changed")
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM events WHERE run_id=? AND seq=?", (self.rid, self.seq))
        with self.assertRaises(NotFound):
            self.store.event_payload(self.rid, self.seq, self.sha)

    def test_external_edit_and_corrupt_text_cannot_reuse_cached_payload(self):
        self.store.event_payload(self.rid, self.seq, self.sha)
        with closing(sqlite3.connect(Path(self.temp.name) / "lab.db")) as db, db:
            db.execute("UPDATE events SET payload='[]' WHERE run_id=? AND seq=?", (self.rid, self.seq))
        with self.assertRaises(LabError) as corrupt:
            self.store.event_payload(self.rid, self.seq, self.sha)
        self.assertEqual(corrupt.exception.code, "invalid_trace")
        self.assertEqual(self.store.events(self.rid, tolerant=True, preview=True)[0]["kind"], "event_corrupt")

    def test_capacity_eviction_and_oversize_bypass(self):
        with patch("lab.storage.PAYLOAD_CACHE_ENTRIES", 2), patch("lab.storage.PAYLOAD_CACHE_BYTES", 1024):
            self.store.event_payload(self.rid, self.seq, self.sha)
            self.assertEqual(self.store.payload_cache_bytes, 0)
            for i in range(3):
                seq = self.store.event(self.rid, "small", {"i":i})
                self.store.event_payload(self.rid, seq, content_hash(canonical({"i":i})))
            self.assertEqual(len(self.store.payload_cache), 2)
            self.assertLessEqual(self.store.payload_cache_bytes, 1024)

    def test_external_change_during_hash_is_not_cached(self):
        def changed(text):
            with closing(sqlite3.connect(Path(self.temp.name) / "lab.db")) as db, db:
                db.execute("UPDATE events SET payload='{}' WHERE run_id=? AND seq=?", (self.rid, self.seq))
            return content_hash(text)
        with patch("lab.storage.content_hash", side_effect=changed):
            self.store.event_payload(self.rid, self.seq, self.sha)
        self.assertFalse(self.store.payload_cache)
        with self.assertRaises(LabError) as updated:
            self.store.event_payload(self.rid, self.seq, self.sha)
        self.assertEqual(updated.exception.code, "payload_changed")


if __name__ == "__main__":
    unittest.main()

import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from lab.app import create_app
from lab.common import ROOT, NotFound, canonical, content_hash
from lab.runner import Runner
from lab.storage import Store


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "data/verification").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data/verification")
        self.store = Store(self.temp.name)
        self.runner = Runner(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def completed(self, repeats=1):
        eid = self.runner.create_experiment(["N01"], repeats=repeats)
        self.runner.execute_experiment(eid)
        return eid, self.store.experiment_page(eid)

    def test_warm_page_reuses_snapshot_without_leaking_mutations(self):
        eid, page = self.completed()
        self.store.run(page["runs"][0]["id"], force=True)
        hits = self.store.experiment_cache_hits
        page["runs"][0]["metadata"]["case_hash"] = "changed"
        page["summary"]["contract_pass"] = -1
        with patch.object(self.store, "run", side_effect=AssertionError("warm page reloaded runs")):
            fresh = self.store.experiment_page(eid)
            self.assertEqual(fresh["summary"]["contract_pass"], 1)
            self.assertNotEqual(fresh["runs"][0]["metadata"]["case_hash"], "changed")
        self.assertEqual(self.store.experiment_cache_hits, hits + 1)

    def test_report_change_invalidates_cached_summary_and_page(self):
        eid, page = self.completed()
        rid = page["runs"][0]["id"]
        (Path(self.temp.name) / "runs" / rid / "report.json").write_text("{}", encoding="utf-8")
        fresh = self.store.experiment_page(eid)
        self.assertEqual(fresh["summary"]["contract_pass"], 0)
        self.assertEqual(fresh["runs"][0]["integrity"]["state"], "invalid")
        self.assertIsNone(fresh["runs"][0]["evaluation"])

    def test_external_connection_event_change_invalidates_snapshot(self):
        eid, page = self.completed()
        rid = page["runs"][0]["id"]
        with closing(sqlite3.connect(Path(self.temp.name) / "lab.db")) as other, other:
            other.execute("UPDATE events SET payload='{}' WHERE run_id=? AND seq=1", (rid,))
        fresh = self.store.experiment_page(eid)
        self.assertEqual(fresh["runs"][0]["integrity"]["reason"], "trace_hash_mismatch")
        self.assertEqual(fresh["summary"]["contract_pass"], 0)

    def test_appended_evaluation_invalidates_summary(self):
        eid, page = self.completed()
        run = page["runs"][0]
        evaluation = deepcopy(run["evaluation"])
        evaluation.update(contract_pass=False, complete_success=False)
        self.store.add_evaluation(run["id"], evaluation, "local-test")
        fresh = self.store.experiment_page(eid)
        self.assertEqual(fresh["summary"]["contract_pass"], 0)
        self.assertTrue(fresh["runs"][0]["historical_evaluation"]["contract_pass"])

    def test_changed_artifact_path_cannot_escape_cached_directory(self):
        eid, page = self.completed()
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE artifacts SET path='../outside/report.json' WHERE run_id=?",
                                  (page["runs"][0]["id"],))
        with self.assertRaises(ValueError):
            self.store.experiment_page(eid)

    def test_active_bypass_force_bypass_and_cache_bound(self):
        pending = self.runner.create_experiment(["N01"])
        self.store.experiment_page(pending)
        self.assertNotIn(pending, self.store.experiment_cache)
        for _ in range(9):
            eid, _ = self.completed()
        self.assertEqual(len(self.store.experiment_cache), 8)
        with patch.object(self.store, "verify_run", wraps=self.store.verify_run) as verify:
            self.store.experiment_info(eid, force=True)
            self.assertTrue(all(call.kwargs.get("force", call.args[1] if len(call.args) > 1 else False)
                                for call in verify.call_args_list))
        self.assertNotIn(eid, self.store.experiment_cache)

    def test_page_cursor_and_summary_cover_all_runs(self):
        eid, _ = self.completed(repeats=3)
        first = self.store.experiment_page(eid, limit=2)
        last = self.store.experiment_page(eid, limit=2, after=first["next_run_after"])
        self.assertEqual(first["summary"]["contract_pass"], 3)
        self.assertEqual(last["run_count"], 3)
        self.assertIsNone(last["next_run_after"])
        self.assertEqual(len({r["id"] for r in first["runs"] + last["runs"]}), 3)
        with self.assertRaises(NotFound):
            self.store.experiment_page(eid, after="wrong")


class PayloadApiTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "data/verification").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data/verification")
        self.app = create_app(self.temp.name)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()
        self.store = self.app.state.store
        eid = self.app.state.runner.create_experiment(["N01"])
        self.rid = self.store.experiment_info(eid)["runs"][0]["id"]
        self.payload = {"content": "资料🙂𠀀" * 3000}
        self.store.event(self.rid, "large_probe", self.payload)
        self.path = "/api/runs/" + self.rid + "/events"
        self.sha = content_hash(canonical(self.payload))

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_preview_and_unicode_chunks_reassemble_exact_original(self):
        preview = self.client.get(self.path + "?preview=true").json()[0]
        self.assertNotIn("payload", preview)
        self.assertEqual(len(preview["payload_preview"]), 512)
        self.assertEqual(preview["payload_sha256"], self.sha)
        self.assertTrue(preview["payload_truncated"])
        chunks, offset = [], 0
        while offset is not None:
            response = self.client.get(self.path + "/1/payload", params={"sha256": self.sha, "offset": offset})
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertLessEqual(len(result["text"]), 4096)
            chunks.append(result["text"])
            offset = result["next_offset"]
        raw = "".join(chunks)
        self.assertEqual(content_hash(raw), self.sha)
        self.assertEqual(json.loads(raw), self.payload)
        self.assertEqual(self.client.get(self.path).json()[0]["payload"], self.payload)

    def test_chunk_boundaries_errors_and_payload_change(self):
        endpoint = self.path + "/1/payload"
        for parameters in ({"limit":4097}, {"offset":-1}, {"sha256":"bad"}):
            self.assertEqual(self.client.get(endpoint, params={"sha256":self.sha, **parameters}).status_code, 422)
        self.assertEqual(self.client.get(endpoint, params={"sha256":self.sha,"offset":999999}).status_code, 400)
        self.assertEqual(self.client.get(self.path + "/99/payload", params={"sha256":self.sha}).status_code, 404)
        end = self.client.get(endpoint, params={"sha256":self.sha,"offset":len(canonical(self.payload))}).json()
        self.assertEqual(end["text"], "")
        self.assertIsNone(end["next_offset"])
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE events SET payload='{}' WHERE run_id=? AND seq=1", (self.rid,))
        changed = self.client.get(endpoint, params={"sha256":self.sha})
        self.assertEqual((changed.status_code, changed.json()["code"]), (409, "payload_changed"))

    def test_corrupt_payload_diagnostic_and_invalid_run_rejection(self):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE events SET payload='[]' WHERE run_id=? AND seq=1", (self.rid,))
        preview = self.client.get(self.path + "?preview=true").json()[0]
        self.assertEqual(preview["kind"], "event_corrupt")
        self.assertEqual(preview["payload"]["raw_sha256"], content_hash("[]"))
        corrupt = self.client.get(self.path + "/1/payload", params={"sha256":self.sha})
        self.assertEqual((corrupt.status_code, corrupt.json()["code"]), (409, "invalid_trace"))
        self.store.finish(self.rid, "failed")
        self.assertEqual(self.client.get(self.path + "/1/payload", params={"sha256":self.sha}).status_code, 409)

    def test_small_preview_and_missing_run_validation(self):
        self.store.event(self.rid, "small_probe", {"value":1})
        preview = self.client.get(self.path + "?preview=true&after_seq=1&limit=1").json()[0]
        self.assertFalse(preview["payload_truncated"])
        self.assertEqual(json.loads(preview["payload_preview"]), {"value":1})
        self.assertEqual(self.client.get("/api/runs/missing/events/1/payload", params={"sha256":self.sha}).status_code, 404)


if __name__ == "__main__":
    unittest.main()

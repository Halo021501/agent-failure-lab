import tempfile
import time
import unittest
import threading
import csv
import io
import os
from unittest.mock import patch
from pathlib import Path
from fastapi.testclient import TestClient
from lab.app import create_app
from lab.common import ROOT
from lab.model_config import load_model_config, save_model_config


class ApiTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "data/verification").mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data/verification")
        self.app = create_app(self.temp.name)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def wait_experiment(self, experiment_id):
        for _ in range(100):
            result = self.client.get("/api/experiments/" + experiment_id).json()
            if all(r["status"] not in ("pending", "running") for r in result["runs"]):
                return result
            time.sleep(.02)
        self.fail("Experiment did not terminate")

    def test_model_configuration_rejects_secret_and_persists_nonsecret(self):
        path = Path(self.temp.name) / "model.json"
        save_model_config(load_model_config().to_dict(), path)
        with patch("lab.app.load_model_config", side_effect=lambda:load_model_config(path)), patch("lab.app.save_model_config", side_effect=lambda values:save_model_config(values,path)):
            current = self.client.get("/api/model-config").json()
            self.assertNotIn("api_key", current)
            values = {k:v for k,v in current.items() if k not in ("configured","credential_present","reason")}
            values["model"] = "mock-model"
            self.assertEqual(self.client.post("/api/model-config",json=values).status_code,200)
            self.assertEqual(load_model_config(path).model,"mock-model")
            self.assertEqual(self.client.post("/api/model-config",json=dict(values,api_key="test-only")).status_code,400)
            self.assertNotIn("test-only",path.read_text(encoding="utf-8"))

    def test_model_experiment_requires_configuration_before_queueing(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/api/experiments",json={"case_ids":["N01"],"agent_kind":"model","strategy":"model-recovery"})
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()["code"],"model_not_configured")
        self.assertEqual(self.app.state.store.pending_count(),0)

    def test_model_route_records_simulated_usage_and_valid_failure(self):
        from lab.model_config import ModelConfig
        from test_model import FakeTransport, response
        fake = FakeTransport([response()])
        runner = self.app.state.runner
        runner.model_config = ModelConfig(model="mock-model",api_key_env="LAB_TEST_CREDENTIAL")
        runner.transport_factory = lambda config:fake
        with patch.dict(os.environ,{"LAB_TEST_CREDENTIAL":"test-only"}):
            result = self.client.post("/api/experiments",json={"case_ids":["N01"],"agent_kind":"model","strategy":"model-minimal"})
            self.assertEqual(result.status_code,202)
            experiment = self.wait_experiment(result.json()["id"])
        run = experiment["runs"][0]
        self.assertEqual(run["metadata"]["model_calls"],1)
        self.assertEqual(run["status"],"failed")
        self.assertEqual(run["integrity"]["state"],"valid")
        self.assertIsNone(experiment["summary"]["cost"])

    def test_local_page_assets_status_and_case_catalog(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertIn("default-src", self.client.get("/").headers["content-security-policy"])
        self.assertEqual(self.client.get("/api/cases").json()["count"], 30)
        self.assertFalse(self.client.get("/api/status").json()["model_configured"])

    def test_suite_catalog_selection_and_shared_queue(self):
        inventory = self.client.get("/api/suites").json()
        self.assertEqual(inventory["default"], "v1")
        self.assertEqual({s["namespace"]:s["count"] for s in inventory["suites"]}, {"v1":30,"heldout-v1":12})
        heldout = self.client.get("/api/cases?suite=heldout-v1").json()
        self.assertEqual(heldout["count"], 12)
        self.assertTrue(all(c["id"].startswith("H") for c in heldout["cases"]))
        self.assertEqual(self.client.get("/api/cases/H01?suite=heldout-v1").status_code,200)
        self.assertEqual(self.client.get("/api/cases/H01").status_code,404)
        self.assertEqual(self.client.get("/api/cases?suite=unknown").status_code,404)
        self.assertEqual(self.client.post("/api/experiments",json={"suite_namespace":"heldout-v1","case_ids":["N01"]}).status_code,400)
        self.assertEqual(self.client.post("/api/experiments",json={"suite_namespace":"unknown"}).status_code,404)
        self.assertEqual(self.app.state.store.pending_count(),0)
        held_runner = self.app.state.runners["heldout-v1"]
        self.assertIs(held_runner.lock,self.app.state.runner.lock)
        self.assertIs(held_runner.cancellations,self.app.state.runner.cancellations)
        created = self.client.post("/api/experiments",json={"suite_namespace":"heldout-v1","case_ids":["H01","H12"]}).json()
        result = self.wait_experiment(created["id"])
        self.assertEqual(result["summary"]["contract_pass"],2)
        self.assertEqual(result["config"]["suite_namespace"],"heldout-v1")
        self.assertEqual(result["config"]["suite_hash"],heldout["hash"])
        source = result["runs"][0]
        for mode in ("trace_replay","tool_replay","fresh_run"):
            response = self.client.post("/api/runs/"+source["id"]+"/replay",json={"mode":mode})
            self.assertEqual(response.status_code,202)
            replay = self.wait_experiment(response.json()["experiment_id"])["runs"][0]
            self.assertEqual(replay["status"],"completed")
            self.assertEqual(replay["metadata"]["suite_namespace"],"heldout-v1")

    def test_corrupt_events_are_browsable_but_never_valid_evidence(self):
        created = self.client.post("/api/experiments",json={"case_ids":["N01"]}).json()
        result = self.wait_experiment(created["id"])
        source = result["runs"][0]
        ref = self.client.get("/api/runs/"+source["id"]).json()["report"]["features"][0]["evidence"][0]
        store = self.app.state.store
        for corrupt in ("{broken", "[]", "null"):
            with store.lock,store.db:
                store.db.execute("UPDATE events SET payload=? WHERE run_id=? AND seq=1",(corrupt,source["id"]))
            detail = self.client.get("/api/runs/"+source["id"]).json()
            self.assertEqual(detail["integrity"]["reason"],"invalid_trace")
            self.assertIsNone(detail["evaluation"])
            events = self.client.get("/api/runs/"+source["id"]+"/events?limit=2").json()
            self.assertEqual(events[0]["kind"],"event_corrupt")
            self.assertEqual(len(events[0]["payload"]["raw_sha256"]),64)
            self.assertEqual(events[1]["seq"],2)
            self.assertEqual(self.client.get("/api/experiments/"+created["id"]).json()["summary"]["contract_pass"],0)
            self.assertIsNone(self.client.get("/api/experiments/"+created["id"]+"/export").json()["runs"][0]["evaluation"])
            evidence = self.client.get("/api/runs/"+source["id"]+"/evidence",params={"source_id":ref["source_id"],"sha256":ref["content_sha256"]})
            self.assertEqual((evidence.status_code,evidence.json()["code"]),(409,"invalid_trace"))
            for mode in ("trace_replay","tool_replay"):
                response = self.client.post("/api/runs/"+source["id"]+"/replay",json={"mode":mode})
                self.assertEqual((response.status_code,response.json()["code"]),(409,"invalid_trace"))
            self.assertEqual(self.client.post("/api/runs/"+source["id"]+"/reevaluate",json={}).status_code,409)
            with store.lock:
                self.assertEqual(store.db.execute("SELECT payload FROM events WHERE run_id=? AND seq=1",(source["id"],)).fetchone()[0],corrupt)

    def test_service_default_suite_and_version_are_consistent(self):
        from lab import __version__
        self.assertEqual(self.client.get("/api/health").json()["version"],__version__)
        self.assertEqual(self.client.get("/api/status").json()["version"],__version__)
        with TestClient(create_app(Path(self.temp.name)/"other",namespace="heldout-v1"),base_url="http://127.0.0.1") as other:
            self.assertEqual(other.get("/api/cases").json()["count"],12)
            self.assertEqual(other.get("/api/status").json()["suite_namespace"],"heldout-v1")

    def test_case_run_events_artifact_replay_compare_and_export(self):
        created = self.client.post("/api/experiments", json={"case_ids": ["F-T01"]})
        self.assertEqual(created.status_code, 202)
        result = self.wait_experiment(created.json()["id"])
        run_id = result["runs"][0]["id"]
        self.assertEqual(result["summary"]["contract_pass"], 1)
        self.assertTrue(self.client.get("/api/runs/" + run_id).json()["report"])
        events = self.client.get("/api/runs/" + run_id + "/events").json()
        self.assertEqual(self.client.get("/api/runs/" + run_id + "/events?after_seq=9999").json(), [])
        self.assertTrue(events)
        self.assertEqual(self.client.get("/api/runs/" + run_id + "/artifacts/report").status_code, 200)
        replay = self.client.post("/api/runs/" + run_id + "/replay", json={"mode": "trace_replay"})
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(self.wait_experiment(replay.json()["experiment_id"])["summary"]["eligible"], 0)
        comparison = self.client.post("/api/compare", json={"left": result["id"], "right": replay.json()["experiment_id"]}).json()
        self.assertFalse(comparison["comparable"])
        export = self.client.get("/api/experiments/" + result["id"] + "/export?format=csv")
        self.assertIn("case_id", export.text)
        self.assertIn("attachment", export.headers["content-disposition"])
        self.assertEqual(self.client.get("/api/experiments/" + result["id"] + "/export?format=xml").status_code, 400)

    def test_invalid_inputs_origins_and_hosts_do_not_create_experiments(self):
        for payload in ({"case_ids": []}, {"strategy": "model"}, {"repeats": True}, {"budget": {"max_tool_calls": 0}}, {"budget": {"unknown": 1}}):
            self.assertIn(self.client.post("/api/experiments", json=payload).status_code, (400, 422))
        self.assertEqual(self.client.post("/api/experiments", json={}, headers={"Origin": "https://example.com"}).status_code, 403)
        self.assertEqual(self.client.get("/api/health", headers={"Host": "example.com"}).status_code, 403)
        self.assertEqual(self.client.get("/api/experiments").json(), [])

    def test_cancel_entire_queued_experiment(self):
        result = self.client.post("/api/experiments", json={"repeats": 3}).json()
        stopped = self.client.post("/api/experiments/" + result["id"] + "/cancel", json={})
        self.assertEqual(stopped.status_code, 200)
        result = self.wait_experiment(result["id"])
        self.assertTrue(any(r["status"] == "cancelled" for r in result["runs"]))
        first_events = {r["id"]: len(self.app.state.store.events(r["id"])) for r in result["runs"]}
        time.sleep(.05)
        self.assertEqual(first_events, {r["id"]: len(self.app.state.store.events(r["id"])) for r in result["runs"]})

    def test_replay_returns_202_while_worker_is_busy(self):
        result = self.client.post("/api/experiments", json={"case_ids": ["N01"]}).json()
        source = self.wait_experiment(result["id"])["runs"][0]
        gate, entered = threading.Event(), threading.Event()
        def block():
            entered.set()
            gate.wait(5)
        future = self.app.state.executor.submit(block)
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            response = self.client.post("/api/runs/" + source["id"] + "/replay", json={"mode": "tool_replay"})
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(response.status_code, 202)
            replay = self.client.get("/api/runs/" + response.json()["run_id"]).json()
            self.assertEqual(replay["status"], "pending")
        finally:
            gate.set()
            future.result()
        self.assertEqual(self.wait_experiment(response.json()["experiment_id"])["runs"][0]["status"], "completed")

    def test_integrity_is_consistent_in_detail_summary_compare_and_exports(self):
        created = self.client.post("/api/experiments", json={"case_ids": ["N01"]}).json()
        result = self.wait_experiment(created["id"])
        run_id = result["runs"][0]["id"]
        (Path(self.temp.name) / "runs" / run_id / "report.json").write_text("{}", encoding="utf-8")
        detail = self.client.get("/api/runs/" + run_id).json()
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(detail["integrity"]["state"], "invalid")
        self.assertIsNone(detail["evaluation"])
        self.assertTrue(detail["historical_evaluation"]["contract_pass"])
        self.assertEqual(self.client.get("/api/experiments/" + created["id"]).json()["summary"]["contract_pass"], 0)
        exported = self.client.get("/api/experiments/" + created["id"] + "/export").json()
        self.assertIsNone(exported["runs"][0]["evaluation"])
        csv_result = self.client.get("/api/experiments/" + created["id"] + "/export?format=csv")
        rows = list(csv.DictReader(io.StringIO(csv_result.text.lstrip("\ufeff"))))
        self.assertEqual(rows[0]["integrity"], "invalid")
        self.assertEqual(rows[0]["contract_pass"], "")
        self.assertEqual(rows[0]["historical_contract_pass"], "True")
        compared = self.client.post("/api/compare", json={"left": created["id"], "right": created["id"]}).json()
        self.assertFalse(compared["comparable"])
        self.assertIsNone(compared["pairs"][0]["change"])

    def test_actual_evidence_and_append_only_reevaluation(self):
        created = self.client.post("/api/experiments", json={"case_ids": ["N01"]}).json()
        run_id = self.wait_experiment(created["id"])["runs"][0]["id"]
        detail = self.client.get("/api/runs/" + run_id).json()
        ref = detail["report"]["features"][0]["evidence"][0]
        evidence = self.client.get("/api/runs/" + run_id + "/evidence", params={"source_id":ref["source_id"], "sha256":ref["content_sha256"]})
        self.assertEqual(evidence.status_code, 200)
        self.assertIn(detail["report"]["features"][0]["text"], evidence.json()["content"])
        self.assertEqual(self.client.get("/api/runs/" + run_id + "/evidence", params={"source_id":"../evals", "sha256":"0"*64}).status_code, 404)
        original_events = self.client.get("/api/runs/" + run_id + "/events").json()
        result = self.client.post("/api/runs/" + run_id + "/reevaluate", json={})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.client.get("/api/runs/" + run_id).json()["evaluation_history"]), 2)
        self.assertEqual(self.client.get("/api/runs/" + run_id + "/events").json(), original_events)

    def test_paginated_lists_events_and_not_found_errors(self):
        runner, store = self.app.state.runner, self.app.state.store
        for _ in range(105):
            runner.create_experiment(["N01"])
        first = self.client.get("/api/experiments?limit=100").json()
        second = self.client.get("/api/experiments", params={"limit":100,"before":first[-1]["id"]}).json()
        self.assertEqual(len(first) + len(second), 105)
        run_id = store.experiment_info(first[0]["id"])["runs"][0]["id"]
        with store.lock, store.db:
            for number in range(10000):
                store._event(run_id, "probe", {"number":number})
        events = self.client.get("/api/runs/" + run_id + "/events?limit=100").json()
        self.assertEqual(len(events), 100)
        self.assertEqual(self.client.get("/api/runs/" + run_id + "/events?after_seq=9999").json()[0]["seq"], 10000)
        self.assertEqual(self.client.get("/api/runs/" + run_id + "/events?limit=201").status_code, 422)
        for resource in ("runs/missing", "experiments/missing", "cases/missing"):
            response = self.client.get("/api/" + resource)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["code"], "not_found")


if __name__ == "__main__":
    unittest.main()

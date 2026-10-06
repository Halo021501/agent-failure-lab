import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from lab.cases import Suite
from lab.common import ROOT, confined, digest, LabError
from lab.compare import compare, summary
from lab.evaluate import evaluate
from lab.gateway import Gateway
from lab.replay import normalized_events
from lab.runner import Runner
from lab.schemas import Budget
from lab.storage import Store
from lab.tools import Tools


class LabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = Suite()
        (ROOT / "data/verification").mkdir(parents=True, exist_ok=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data/verification")
        self.store = Store(self.temp.name)
        self.runner = Runner(self.store, self.suite)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_case(self, case_id="N01", strategy="recovery", budget=None):
        exp_id = self.runner.create_experiment([case_id], strategy, budget=budget)
        result = self.runner.execute_experiment(exp_id)
        return result, result["runs"][0]

    def test_suite_distribution_and_version(self):
        cases = list(self.suite.cases.values())
        self.assertEqual(len(cases), 30)
        self.assertEqual(sum(c.definition["category"] == "normal" for c in cases), 6)
        self.assertEqual(sum(bool(c.definition["faults"]) and c.definition["recoverable"] for c in cases), 19)
        self.assertEqual(sum(not c.definition["recoverable"] for c in cases), 5)
        self.assertEqual(Suite().hash, self.suite.hash)

    def test_suite_rejects_duplicate_sources_and_invalid_rules(self):
        definition = copy.deepcopy(self.suite.get("N01").definition)
        definition["sources"].append(definition["sources"][0])
        with self.assertRaises(ValueError):
            Suite.validate(definition)
        definition = copy.deepcopy(self.suite.get("F-T01").definition)
        definition["faults"][0]["occurrences"] = [True]
        with self.assertRaises(ValueError):
            Suite.validate(definition)
        definition["faults"][0]["occurrences"] = [1]
        definition["faults"][0]["phase"] = "after"
        with self.assertRaises(ValueError):
            Suite.validate(definition)

    def test_path_and_truth_access_are_rejected(self):
        with self.assertRaises(ValueError):
            confined(ROOT / "cases/v1/public", "../../../../README.md")
        experiment = self.runner.create_experiment(["N01"])
        run = self.store.experiment_info(experiment)["runs"][0]
        tools = Tools(self.suite.get("N01"), self.store, run["id"])
        with self.assertRaises(LabError):
            tools.execute("read_source", {"source_id": "../../../evals/v1/expected/N01.json"})
        with self.assertRaises(LabError):
            tools.execute("read_source", {"source_id": "primary-release", "path": "../expected"})
        index = tools.execute("list_sources", {"scope": "primary"})
        self.assertFalse(any("expected" in s["id"] for s in index["sources"]))

    def test_all_30_recovery_contracts(self):
        experiment = self.runner.create_experiment()
        result = self.runner.execute_experiment(experiment)
        for run in result["runs"]:
            with self.subTest(case=run["case_id"]):
                self.assertEqual(run["status"], "completed")
                self.assertTrue(run["evaluation"]["contract_pass"], run["evaluation"])
        metrics = summary(result["runs"])
        self.assertEqual((metrics["contract_pass"], metrics["complete"], metrics["recovered"]), (30, 25, 19))
        self.assertEqual(metrics["model_calls"], 0)
        self.assertEqual(metrics["fault_coverage"], 1)

    def test_no_recovery_control_fails_recoverable_fault(self):
        _, run = self.run_case("F-T01", "no-recovery")
        self.assertFalse(run["evaluation"]["contract_pass"])
        self.assertFalse(run["evaluation"]["complete_success"])
        self.assertIn("F-T01-timeout", run["metadata"]["activated_faults"])

    def test_duplicate_and_stale_deliveries_are_rejected(self):
        for case_id, expected_decision in (("F-D01", "duplicate_rejected"), ("F-D03", "stale_rejected")):
            _, run = self.run_case(case_id)
            events = self.store.events(run["id"])
            self.assertTrue(any(e["kind"] == "tool_delivery" and e["payload"]["decision"] == expected_decision for e in events))
            self.assertTrue(run["evaluation"]["contract_pass"])

    def test_commit_lost_ack_retries_same_key_once(self):
        for case_id in ("F-D04", "X03"):
            _, run = self.run_case(case_id)
            events = self.store.events(run["id"])
            self.assertEqual(sum(e["kind"] == "artifact_committed" for e in events), 1)
            writes = [e["payload"]["args"] for e in events if e["kind"] == "tool_call" and e["payload"]["tool"] == "write_report"]
            self.assertEqual(len(writes), 2)
            self.assertEqual(writes[0], writes[1])
            self.assertTrue(run["evaluation"]["contract_pass"])

    def test_idempotency_conflict_never_overwrites(self):
        _, run = self.run_case()
        original = self.store.report(run["id"])
        changed = copy.deepcopy(original)
        changed["release_version"] = "fake"
        with self.assertRaises(LabError) as caught:
            self.store.save_report(run["id"], changed, "release-report-v1")
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        with self.assertRaises(LabError):
            self.store.save_report(run["id"], original, "different-key")
        self.assertEqual(self.store.report(run["id"]), original)

    def test_legal_empty_list_and_unavailable_source_differ(self):
        _, clean = self.run_case("N05")
        _, missing = self.run_case("F-M04")
        self.assertEqual(self.store.report(clean["id"])["unresolved_issues"], [])
        self.assertIsNone(self.store.report(missing["id"])["unresolved_issues"])
        self.assertTrue(missing["evaluation"]["contract_pass"])
        self.assertFalse(missing["evaluation"]["complete_success"])

    def test_evaluator_detects_wrong_facts_and_missing_citations(self):
        _, run = self.run_case()
        report, events = self.store.report(run["id"]), self.store.events(run["id"])
        mutations = []
        wrong = copy.deepcopy(report); wrong["release_version"] = "9.9.9"; mutations.append(wrong)
        wrong = copy.deepcopy(report); wrong["features"][0]["evidence"] = []; mutations.append(wrong)
        wrong = copy.deepcopy(report); wrong["features"] = []; mutations.append(wrong)
        wrong = copy.deepcopy(report); wrong["test_summary"]["evidence"] = wrong["test_summary"]["evidence"][:1]; mutations.append(wrong)
        for mutation in mutations:
            self.assertFalse(evaluate(self.suite.get("N01"), mutation, events)["contract_pass"])

    def test_evaluator_detects_forged_hash_locator_and_unread_source(self):
        _, run = self.run_case()
        report, events = self.store.report(run["id"]), self.store.events(run["id"])
        for field, value in (("content_sha256", "0" * 64), ("source_id", "backup-release"), ("locator", {"kind": "line", "line": 999}), ("locator", {"kind": "line", "line": -1})):
            mutation = copy.deepcopy(report)
            mutation["features"][0]["evidence"][0][field] = value
            self.assertFalse(evaluate(self.suite.get("N01"), mutation, events)["contract_pass"])

    def test_partial_cannot_invent_gap_or_claim_complete(self):
        _, run = self.run_case("F-T04")
        report, events = self.store.report(run["id"]), self.store.events(run["id"])
        report["overall_status"] = "complete"
        self.assertFalse(evaluate(self.suite.get("F-T04"), report, events)["contract_pass"])
        report["overall_status"] = "partial"
        report["gaps"][0]["attempts"] = 123
        self.assertFalse(evaluate(self.suite.get("F-T04"), report, events)["contract_pass"])

    def test_trace_replay_exact_events_and_zero_execution(self):
        _, source = self.run_case("X03")
        exp_id, replay_id = self.runner.replay(source["id"])
        replay = self.store.run(replay_id)
        self.assertEqual(normalized_events(self.store.events(source["id"])), normalized_events(self.store.events(replay_id)))
        self.assertEqual(self.store.report(source["id"]), self.store.report(replay_id))
        self.assertEqual(replay["metadata"]["executed_tool_calls"], 0)
        self.assertEqual(replay["metadata"]["executed_model_calls"], 0)
        self.assertEqual(summary(self.store.experiment_info(exp_id)["runs"])["eligible"], 0)

    def test_tool_replay_matches_faults_and_idempotent_submit(self):
        for case_id in ("F-T01", "F-D04", "X01", "F-M04"):
            _, source = self.run_case(case_id)
            _, replay_id = self.runner.replay(source["id"], "tool_replay")
            replay = self.store.run(replay_id)
            self.assertEqual(replay["status"], "completed", replay)
            self.assertTrue(replay["evaluation"]["contract_pass"], replay["evaluation"])
            self.assertEqual(replay["metadata"]["executed_tool_calls"], 0)

    def test_tool_replay_changed_decisions_stop_explicitly(self):
        _, source = self.run_case("F-T01")
        _, replay_id = self.runner.replay(source["id"], "tool_replay", "no-recovery")
        replay = self.store.run(replay_id)
        self.assertEqual(replay["status"], "failed")
        self.assertEqual(replay["metadata"]["error_code"], "replay_mismatch")
        self.assertIsNone(replay["evaluation"])

    def test_trace_event_tampering_is_detected_before_replay(self):
        _, source = self.run_case("N01")
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE events SET payload=? WHERE run_id=? AND seq=1", ('{"tampered":true}', source["id"]))
        for mode in ("trace_replay", "tool_replay"):
            with self.assertRaises(LabError) as caught:
                self.runner.replay(source["id"], mode)
            self.assertEqual(caught.exception.code, "trace_hash_mismatch")

    def test_fresh_run_is_deterministic_except_metadata(self):
        _, source = self.run_case("X01")
        _, next_id = self.runner.replay(source["id"], "fresh_run")
        self.assertEqual(normalized_events(self.store.events(source["id"])), normalized_events(self.store.events(next_id)))
        self.assertEqual(self.store.report(source["id"]), self.store.report(next_id))
        self.assertNotEqual(source["id"], next_id)

    def test_tool_budget_and_wall_budget_have_explicit_states(self):
        _, run = self.run_case(budget=Budget(max_tool_calls=1))
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(run["metadata"]["tool_calls"], 1)
        exp_id = self.runner.create_experiment(["N01"], budget=Budget(wall_seconds=.01))
        run_id = self.store.experiment_info(exp_id)["runs"][0]["id"]
        run = self.runner.execute_run(run_id, on_call=lambda g: time.sleep(.03))
        self.assertEqual(run["status"], "budget_exceeded")

    def test_real_tool_deadline_is_detected(self):
        exp_id = self.runner.create_experiment(["N01"])
        run_id = self.store.experiment_info(exp_id)["runs"][0]["id"]
        gateway = Gateway(self.suite.get("N01"), self.store, run_id, Budget(tool_timeout_seconds=.001), threading.Event())
        original = gateway.tools.execute
        def slow(tool, args):
            time.sleep(.01)
            return original(tool, args)
        gateway.tools.execute = slow
        with self.assertRaises(LabError) as caught:
            gateway.call("list_sources", {"scope": "primary"})
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(any(e["kind"] == "tool_error" for e in self.store.events(run_id)))

    def test_cancel_at_tool_boundary_stops_future_calls(self):
        exp_id = self.runner.create_experiment(["N01"])
        run_id = self.store.experiment_info(exp_id)["runs"][0]["id"]
        def stop(gateway):
            if gateway.tool_calls == 3:
                gateway.cancel.set()
        run = self.runner.execute_run(run_id, on_call=stop)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["metadata"]["tool_calls"], 3)
        self.assertFalse(any(e["kind"] == "artifact_committed" for e in self.store.events(run_id)))

    def test_restart_marks_unfinished_run_interrupted(self):
        exp_id = self.runner.create_experiment(["N01"])
        run_id = self.store.experiment_info(exp_id)["runs"][0]["id"]
        self.store.finish(run_id, "running")
        self.store.close()
        self.store = Store(self.temp.name)
        self.assertEqual(self.store.run(run_id)["status"], "interrupted")

    def test_artifact_tampering_invalidates_replay_and_startup_result(self):
        _, run = self.run_case()
        path = Path(self.temp.name) / "runs" / run["id"] / "report.json"
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(LabError) as caught:
            self.runner.replay(run["id"])
        self.assertEqual(caught.exception.code, "artifact_hash_mismatch")
        self.store.close()
        self.store = Store(self.temp.name)
        damaged = self.store.run(run["id"])
        self.assertEqual(damaged["status"], "completed")
        self.assertEqual(damaged["integrity"]["state"], "invalid")
        self.assertTrue(damaged["historical_evaluation"]["contract_pass"])
        self.assertIsNone(damaged["evaluation"])

    def test_compare_requires_equal_conditions(self):
        left, _ = self.run_case("F-T01", "no-recovery")
        right, _ = self.run_case("F-T01")
        result = compare(left, right)
        self.assertTrue(result["comparable"])
        self.assertEqual(result["pairs"][0]["change"], "improved")
        right["config"]["budget"]["max_tool_calls"] += 1
        self.assertFalse(compare(left, right)["comparable"])

    def test_no_fault_denominator_is_na_and_unused_fault_not_recovery(self):
        result, _ = self.run_case("N01")
        self.assertIsNone(summary(result["runs"])["recovery_rate"])
        result, run = self.run_case("F-T01")
        run["metadata"]["activated_faults"] = []
        metrics = summary([run])
        self.assertIsNone(metrics["recovery_rate"])
        self.assertEqual(metrics["fault_coverage"], 0)

    def test_invalid_experiment_and_budget_rejected_before_queue(self):
        for kwargs in ({"case_ids": []}, {"case_ids": ["N01", "N01"]}, {"case_ids": ["N999"]}, {"repeats": True}, {"strategy": "model"}):
            with self.assertRaises(ValueError):
                self.runner.create_experiment(**kwargs)
        for kwargs in ({"max_tool_calls": True}, {"wall_seconds": float("nan")}, {"wall_seconds": 0}, {"max_read_attempts": 11}):
            with self.assertRaises(ValueError):
                Budget(**kwargs)


if __name__ == "__main__":
    unittest.main()

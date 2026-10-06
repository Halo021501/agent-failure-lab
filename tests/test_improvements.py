import copy
from pathlib import Path
import tempfile
import unittest
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from lab.common import ROOT, LabError
from lab.compare import compare, summary
from lab.runner import Runner
from lab.storage import Store


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data/verification")
        self.store = Store(self.temp.name)
        self.runner = Runner(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_case(self, case="N01", strategy="recovery"):
        experiment_id = self.runner.create_experiment([case], strategy)
        experiment = self.runner.execute_experiment(experiment_id)
        return experiment, experiment["runs"][0]

    def test_a01_corrupt_report_cannot_reuse_cached_pass(self):
        experiment, run = self.run_case()
        (Path(self.temp.name) / "runs" / run["id"] / "report.json").write_text("{}", encoding="utf-8")
        result = self.store.experiment_info(experiment["id"])
        self.assertEqual(summary(result["runs"])["contract_pass"], 0)
        self.assertIsNone(result["runs"][0]["evaluation"])
        self.assertEqual(result["runs"][0]["status"], "completed")
        self.assertTrue(result["runs"][0]["historical_evaluation"]["contract_pass"])

    def test_a02_unstarted_cancel_is_not_an_execution_sample(self):
        _, run = self.run_case()
        queued = self.runner.create_experiment(["N02"], repeats=3)
        for pending in self.store.experiment_info(queued)["runs"]:
            self.runner.cancel(pending["id"])
        runs = [run] + self.store.experiment_info(queued)["runs"]
        result = summary(runs)
        self.assertEqual(result["contract_rate"], 1)
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["p50_seconds"], run["metadata"]["wall_seconds"])
        self.assertEqual(result["execution_coverage"], .25)

    def test_a03_tool_replay_preserves_failed_commit(self):
        for case in ("F-D04", "X03"):
            _, source = self.run_case(case, "no-recovery")
            self.assertFalse(source["evaluation"]["contract_pass"])
            self.assertFalse(source["evaluation"]["complete_success"])
            self.assertEqual(source["evaluation"]["facts_correct"], source["evaluation"]["facts_total"])
            self.assertEqual(source["evaluation"]["facts_missing"], 0)
            _, replay_id = self.runner.replay(source["id"], "tool_replay")
            replay = self.store.run(replay_id)
            self.assertEqual(replay["status"], "failed")
            self.assertEqual(self.store.report(source["id"]), self.store.report(replay_id))
            self.assertEqual(sum(e["kind"] == "artifact_committed" for e in self.store.events(replay_id)), 1)

    def test_a04_framework_changes_and_incomparable_labels(self):
        left, _ = self.run_case("F-T01", "no-recovery")
        right, _ = self.run_case("F-T01")
        changed = copy.deepcopy(right)
        changed["config"]["framework_fingerprint"] = "another-framework"
        self.assertFalse(compare(left, changed)["comparable"])
        changed = copy.deepcopy(right)
        changed["config"]["budget"]["max_tool_calls"] += 1
        result = compare(left, changed)
        self.assertFalse(result["comparable"])
        self.assertTrue(all(row["change"] is None for row in result["pairs"]))

    def test_commit_crash_windows_recover_once(self):
        _, normal = self.run_case()
        report = self.store.report(normal["id"])
        for stage in ("intent", "temp_written", "replaced", "indexed"):
            with tempfile.TemporaryDirectory(dir=ROOT / "data/verification") as folder:
                store = Store(folder)
                runner = Runner(store)
                experiment = runner.create_experiment(["N01"])
                run_id = store.experiment_info(experiment)["runs"][0]["id"]
                def interrupt(actual):
                    if actual == stage:
                        raise RuntimeError("simulated process interruption")
                store.commit_hook = interrupt
                with self.assertRaises(RuntimeError):
                    store.save_report(run_id, report, "same-key")
                store.close()
                store = Store(folder)
                try:
                    run = store.run(run_id)
                    self.assertEqual(run["status"], "interrupted")
                    if stage == "intent":
                        self.assertIsNone(store.optional_report(run_id))
                    else:
                        self.assertEqual(store.report(run_id), report)
                        self.assertEqual(sum(e["kind"] == "artifact_committed" for e in store.events(run_id)), 1)
                        store.save_report(run_id, report, "same-key")
                        self.assertEqual(sum(e["kind"] == "artifact_committed" for e in store.events(run_id)), 1)
                    self.assertEqual(run["integrity"]["state"], "valid")
                finally:
                    store.close()

    def test_schema_v1_backup_preserves_database_and_artifacts(self):
        _, run = self.run_case()
        path = Path(self.temp.name) / "lab.db"
        original = self.store.report(run["id"])
        self.store.close()
        with sqlite3.connect(path) as db:
            for table in ("integrity", "audits", "evaluations", "commits"):
                db.execute("DROP TABLE " + table)
            db.execute("PRAGMA user_version=1")
        db.close()
        self.store = Store(self.temp.name)
        backup = Path(self.store.migration_backup)
        self.assertTrue((backup / "runs" / run["id"] / "report.json").exists())
        with sqlite3.connect(backup / "lab.db") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
        db.close()
        self.assertEqual(self.store.report(run["id"]), original)
        self.assertEqual(self.store.run(run["id"])["status"], "completed")

    def test_failure_without_artifact_can_trace_replay(self):
        from lab.schemas import Budget
        experiment = self.runner.create_experiment(["N01"], budget=Budget(max_tool_calls=1))
        source = self.runner.execute_experiment(experiment)["runs"][0]
        _, replay_id = self.runner.replay(source["id"], "trace_replay")
        self.assertEqual(self.store.run(replay_id)["status"], "budget_exceeded")
        self.assertIsNone(self.store.optional_report(replay_id))
        self.assertEqual(self.store.run(replay_id)["integrity"]["state"], "valid")

    def test_incomplete_tool_record_is_rejected_before_task_creation(self):
        from lab.schemas import Budget
        experiment = self.runner.create_experiment(["N01"], budget=Budget(max_tool_calls=1))
        run_id = self.store.experiment_info(experiment)["runs"][0]["id"]
        source = self.runner.execute_run(run_id, on_call=lambda g: g.cancel.set())
        before = self.store.db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
        with self.assertRaises(LabError) as caught:
            self.runner.prepare_replay(source["id"], "tool_replay")
        self.assertEqual(caught.exception.code, "replay_incomplete")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM experiments").fetchone()[0], before)

    def test_reevaluation_is_append_only_and_audits_do_not_change_trace(self):
        _, run = self.run_case()
        events = self.store.events(run["id"])
        historical = run["historical_evaluation"]
        self.runner.reevaluate(run["id"])
        latest = self.runner.reevaluate(run["id"])
        self.assertEqual(latest["historical_evaluation"], historical)
        self.assertEqual(len(self.store.evaluation_history(run["id"])), 3)
        self.assertEqual(self.store.events(run["id"]), events)
        self.assertEqual(latest["integrity"]["state"], "valid")

    def test_failed_and_budget_runs_remain_in_fact_denominators(self):
        from lab.schemas import Budget
        _, failed = self.run_case("F-D04", "no-recovery")
        experiment = self.runner.create_experiment(["N01"], budget=Budget(max_tool_calls=1))
        budget = self.runner.execute_experiment(experiment)["runs"][0]
        metrics = summary([failed, budget])
        self.assertEqual(metrics["eligible"], 2)
        self.assertEqual(metrics["facts_correct"], 7)  # The acknowledgement failure still produced all seven facts.
        self.assertEqual(metrics["facts_total"], 14)
        self.assertEqual(metrics["facts_missing"], 7)  # The run stopped before reading or writing its report.
        self.assertEqual((metrics["citations_valid"], metrics["citations_total"]), (7, 14))
        self.assertEqual((metrics["contract_pass"], metrics["complete"]), (0, 0))

    def test_duplicate_pairs_and_cancelled_experiments_are_not_comparable(self):
        experiment, _ = self.run_case()
        duplicated = copy.deepcopy(experiment)
        duplicated["runs"].append(copy.deepcopy(duplicated["runs"][0]))
        self.assertIn("duplicate run pairs", compare(experiment, duplicated)["reasons"])
        queued = self.runner.create_experiment(["N01"])
        self.runner.cancel(self.store.experiment_info(queued)["runs"][0]["id"])
        self.assertFalse(compare(experiment, self.store.experiment_info(queued))["comparable"])

    def test_queue_capacity_is_atomic_and_scans_all_experiments(self):
        for _ in range(105):
            self.runner.create_experiment(["N01"])
        self.assertEqual(self.store.pending_count(), 105)
        barrier = threading.Barrier(2)
        def submit():
            barrier.wait()
            try:
                self.runner.create_experiment(repeats=10)
                return True
            except LabError as error:
                self.assertEqual(error.code, "queue_full")
                return False
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(submit) for _ in range(2)]
            self.assertEqual(sum(f.result() for f in futures), 1)
        self.assertEqual(self.store.pending_count(), 405)
        first = self.store.experiments(limit=100)
        second = self.store.experiments(limit=100, before=first[-1]["id"])
        self.assertEqual(len(first) + len(second), 106)
        self.assertFalse({e["id"] for e in first} & {e["id"] for e in second})

    def test_cancel_and_claim_race_never_restarts_terminal_run(self):
        for _ in range(10):
            experiment = self.runner.create_experiment(["N01"])
            run_id = self.store.experiment_info(experiment)["runs"][0]["id"]
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(self.runner.cancel, run_id), executor.submit(self.runner.execute_run, run_id)]
                for future in futures:
                    future.result()
            final = self.store.run(run_id)
            self.assertIn(final["status"], ("cancelled", "completed"))
            self.assertNotIn(run_id, self.runner.cancellations)
            self.assertEqual(self.runner.execute_run(run_id)["status"], final["status"])
            with self.assertRaises(LabError):
                self.store.finish(run_id, "running")

    def test_malformed_event_json_is_invalid_without_destroying_history(self):
        experiment, run = self.run_case()
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE events SET payload='{' WHERE run_id=? AND seq=1", (run["id"],))
        damaged = self.store.run(run["id"], force=True)
        self.assertEqual(damaged["integrity"]["state"], "invalid")
        self.assertEqual(damaged["integrity"]["reason"], "invalid_trace")
        self.assertIsNone(damaged["evaluation"])
        self.assertTrue(damaged["historical_evaluation"]["contract_pass"])

    def test_duplicate_facts_and_references_do_not_inflate_coverage(self):
        from lab.evaluate import evaluate
        _, run = self.run_case()
        report = self.store.report(run["id"])
        report["features"].append(copy.deepcopy(report["features"][0]))
        ev = evaluate(self.runner.suite.get("N01"), report, self.store.events(run["id"]))
        self.assertFalse(ev["contract_pass"])
        self.assertEqual(ev["facts_errors"], 1)
        self.assertLessEqual(ev["citations_valid"], ev["citations_total"])

    def test_legacy_field_counts_are_not_claimed_as_atomic_facts(self):
        experiment, run = self.run_case()
        run["evaluation"] = {"version":"facts-and-citations-v1", "contract_pass":True, "complete_success":True,
                             "facts_correct":4, "facts_total":4, "citations_valid":5, "citations_total":5}
        metrics = summary([run])
        self.assertEqual(metrics["fact_denominator_unknown"], 1)
        self.assertEqual(metrics["facts_total"], 0)
        self.assertEqual(metrics["field_groups_total"], 4)


if __name__ == "__main__":
    unittest.main()

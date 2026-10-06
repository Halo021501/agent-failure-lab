import json
import shutil
import tempfile
import unittest
import time
from pathlib import Path
from lab.cases import Suite
from lab.common import ROOT, LabError, digest
from lab.model_protocol import prompt_fingerprint
from lab.runner import Runner
from lab.storage import Store
from lab.tools import Tools
from lab.schemas import Budget


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "data/verification")
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "runtime")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_frozen_v1_and_heldout_inventories(self):
        self.assertEqual(Suite().hash, "1941f4d20f8bd1f0cab7aa72dbf41ac16c97cf428ee81c5183e1ac339f5e206f")
        inventory = json.loads((ROOT / "docs/HELDOUT_FREEZE.json").read_text(encoding="utf-8"))
        suite = Suite(namespace="heldout-v1")
        self.assertEqual(len(suite.cases), 12)
        self.assertEqual(suite.hash, inventory["suite_hash"])
        self.assertFalse(set(suite.cases) & set(Suite().cases))
        for path, checksum in inventory["files"].items():
            self.assertEqual(digest((ROOT / path).read_text(encoding="utf-8")), checksum)
        for strategy, checksum in inventory["prompt_fingerprints_at_freeze"].items():
            self.assertEqual(prompt_fingerprint(strategy), checksum)

    def test_heldout_script_contracts_without_model_claim(self):
        runner = Runner(self.store, Suite(namespace="heldout-v1"))
        eid = runner.create_experiment()
        result = runner.execute_experiment(eid)
        for run in result["runs"]:
            self.assertTrue(run["evaluation"] and run["evaluation"]["contract_pass"], run)
            self.assertEqual(run["metadata"]["model_calls"], 0)
        self.assertEqual(sum(run["evaluation"]["complete_success"] for run in result["runs"]), 9)

    def test_changed_current_suite_uses_original_snapshot_for_replay_and_reevaluation(self):
        fixture = self.root / "fixture"
        for base in ("cases", "evals"):
            shutil.copytree(ROOT / base / "v1", fixture / base / "v1")
        suite = Suite(fixture)
        runner = Runner(self.store, suite)
        eid = runner.create_experiment(["N01"])
        source = runner.execute_experiment(eid)["runs"][0]
        note = fixture / "cases/v1/public/N01/primary-notes.md"
        note.write_text("Changed future content\n", encoding="utf-8")
        runner.suite = Suite(fixture)
        self.assertNotEqual(runner.suite.hash, source["metadata"]["suite_hash"])
        _, rid = runner.replay(source["id"], "tool_replay")
        self.assertTrue(self.store.run(rid)["evaluation"]["contract_pass"])
        self.assertEqual(self.store.run(rid)["metadata"]["suite_hash"], source["metadata"]["suite_hash"])
        self.assertTrue(runner.reevaluate(source["id"])["evaluation"]["contract_pass"])
        with self.assertRaises(ValueError):
            runner.prepare_replay(source["id"], "fresh_run")

    def test_damaged_snapshot_cannot_be_used(self):
        runner = Runner(self.store)
        eid = runner.create_experiment(["N01"])
        source = runner.execute_experiment(eid)["runs"][0]
        archive = self.store.directory / "suites" / source["metadata"]["suite_hash"]
        (archive / "cases/v1/public/N01/primary-notes.md").write_text("Damage\n", encoding="utf-8")
        with self.assertRaises(LabError) as error:
            runner.prepare_replay(source["id"], "tool_replay")
        self.assertEqual(error.exception.code, "suite_snapshot_mismatch")
        self.assertEqual(self.store.pending_count(), 0)
        moved = archive.with_name(archive.name + "-inspection")
        self.assertTrue(archive.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(moved.resolve().is_relative_to(self.root.resolve()))
        archive.rename(moved)
        with self.assertRaises(LabError) as missing:
            runner.prepare_replay(source["id"], "tool_replay")
        self.assertEqual(missing.exception.code, "suite_snapshot_missing")

    def test_snapshot_truth_is_outside_tool_namespace(self):
        runner = Runner(self.store, Suite(namespace="heldout-v1"))
        eid = runner.create_experiment(["H01"])
        run = self.store.experiment_info(eid)["runs"][0]
        case = runner._run_suite(run["metadata"]).get("H01")
        tools = Tools(case, self.store, run["id"])
        with self.assertRaises(LabError):
            tools.execute("read_source", {"source_id":"../../../../evals/heldout-v1/expected/H01.json"})
        sources = tools.execute("list_sources", {"scope":"primary"})
        self.assertTrue(all("expected" not in item["id"] for item in sources["sources"]))

    def test_wall_budget_includes_snapshot_validation(self):
        runner = Runner(self.store)
        eid = runner.create_experiment(["N01"], budget=Budget(wall_seconds=.01))
        original = runner._run_suite
        def slow_validation(metadata):
            time.sleep(.03)
            return original(metadata)
        runner._run_suite = slow_validation
        run = runner.execute_experiment(eid)["runs"][0]
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(run["metadata"]["tool_calls"], 0)


if __name__ == "__main__":
    unittest.main()

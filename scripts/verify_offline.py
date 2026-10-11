"""Repeatable offline acceptance; all artifacts remain in the named verification folder."""
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab.common import digest
from lab.compare import compare, summary
from lab.replay import normalized_events
from lab.runner import Runner
from lab.storage import Store
from lab import __version__


def main():
    directory = ROOT / "data/verification" / ("acceptance-" + uuid.uuid4().hex[:8])
    store = Store(directory)
    runner = Runner(store)
    try:
        recovery_id = runner.create_experiment(repeats=3)
        recovery = runner.execute_experiment(recovery_id)
        control_id = runner.create_experiment(strategy="no-recovery", repeats=3)
        control = runner.execute_experiment(control_id)
        a, b = summary(recovery["runs"]), summary(control["runs"])
        assert (a["contract_pass"], a["complete"], a["recovered"], a["model_calls"]) == (90, 75, 57, 0), a
        assert b["contract_pass"] < 90 and b["model_calls"] == 0
        replay_count = 0
        failed_replays = 0
        replay_sources = [run for experiment in (recovery, control) for run in sorted(experiment["runs"], key=lambda r:r["case_id"])
                          if run["metadata"]["seed"] == min(experiment["config"]["seeds"])]
        assert len(replay_sources) == 60 and len({(r["experiment_id"],r["case_id"]) for r in replay_sources}) == 60
        for source in replay_sources:
            _, replay_id = runner.replay(source["id"], "trace_replay")
            assert normalized_events(store.events(source["id"])) == normalized_events(store.events(replay_id))
            assert store.optional_report(source["id"]) == store.optional_report(replay_id)
            _, tool_replay_id = runner.replay(source["id"], "tool_replay")
            replay = store.run(tool_replay_id)
            assert replay["status"] == source["status"], replay
            assert bool(replay["evaluation"] and replay["evaluation"]["contract_pass"]) == bool(source["evaluation"] and source["evaluation"]["contract_pass"]), replay
            assert store.optional_report(source["id"]) == store.optional_report(tool_replay_id)
            assert replay["metadata"]["executed_tool_calls"] == 0
            assert replay["integrity"]["state"] == "valid"
            failed_replays += source["status"] == "failed"
            replay_count += 1
        paired = compare(control, recovery)
        assert paired["comparable"] and len(paired["pairs"]) == 90
        for case_id in runner.suite.cases:
            sources = [r for r in recovery["runs"] if r["case_id"] == case_id]
            assert len({digest(normalized_events(store.events(r["id"]))) for r in sources}) == 1
        from lab.provenance import fingerprints
        assert fingerprints()["source_fingerprint"] == runner.fingerprint, "Source changed during acceptance; repeat on frozen code."
        result = {"schema_version": 2, "release": __version__, "passed": True, "source_unchanged_during_checks": True,
                  "suite_version": runner.suite.version, "suite_hash": runner.suite.hash,
                  "source_fingerprint": runner.fingerprint, "data_directory": str(directory),
                  "recovery_experiment": recovery_id, "control_experiment": control_id,
                  "recovery": a, "control": b, "trace_replayed_cases": replay_count,
                  "tool_replayed_cases": replay_count, "deterministic_case_count": 30,
                  "comparison_pairs": len(paired["pairs"]), "model_requests": 0,
                  "failed_source_cases_replayed_in_both_modes": failed_replays,
                  "evaluator_version": recovery["config"]["evaluator_version"],
                  "scope": "script baselines and harness only"}
        (directory / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        (ROOT / "docs/verification-summary-v0.3.3-offline.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()

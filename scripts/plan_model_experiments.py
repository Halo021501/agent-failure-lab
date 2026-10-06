"""Write a reviewable plan only. This program cannot execute model requests."""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab.cases import Suite
from lab.model_config import load_model_config
from lab.model_protocol import STRATEGIES, prompt_fingerprint
from lab.provenance import fingerprints
from lab.schemas import configured_budget


def main():
    config = load_model_config()
    suites = [Suite(), Suite(namespace="heldout-v1")]
    experiments = [{"suite_namespace":suite.namespace, "suite_hash":suite.hash, "strategy":strategy,
                    "prompt_fingerprint":prompt_fingerprint(strategy), "case_ids":sorted(suite.cases), "seeds":[0,1,2],
                    "planned_runs":len(suite.cases) * 3, "budget":configured_budget().to_dict(), "model_config":config.to_dict(),
                    "state":"not_started", "input_token_upper_bound":config.max_batch_input_tokens,
                    "output_token_upper_bound":config.max_batch_output_tokens}
                   for suite in suites for strategy in STRATEGIES]
    plan = {"schema_version":1, "status":"deferred_by_user", "reason":"先不进行消耗额度的真实测试", **fingerprints(),
            "pilot_cases":["N01","F-T01","F-M01","F-S01","F-D04","F-M04"],
            "pilot_status":"not_started", "planned_runs":sum(e["planned_runs"] for e in experiments),
            "planned_pairs":sum(len(suite.cases) * 3 for suite in suites), "executed_model_runs":0,
            "external_model_requests":0, "cost_estimate":None,
            "cost_reason":"No pilot usage or model/pricing configuration; token caps are limits, not estimates.",
            "experiments":experiments}
    path = ROOT / "docs/MODEL_EXPERIMENT_PLAN.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path":str(path),"planned_runs":plan["planned_runs"],"model_requests":0}, ensure_ascii=False))


if __name__ == "__main__":
    main()

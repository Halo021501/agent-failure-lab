import math
from collections import Counter

NATURAL = {"completed", "failed", "budget_exceeded"}


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def summary(runs):
    ability = [r for r in runs if r["metadata"]["mode"] != "trace_replay"]
    valid = [r for r in ability if r.get("integrity", {}).get("state") == "valid"]
    eligible = [r for r in valid if r["status"] in NATURAL]
    evaluated = [r for r in eligible if r["evaluation"]]
    fault_runs = [r for r in eligible if r["metadata"]["fault_ids"]]
    activated = [r for r in fault_runs if r["metadata"].get("activated_faults")]
    recoverable = [r for r in activated if r["metadata"]["recoverable"]]
    complete = sum(bool(r["evaluation"] and r["evaluation"]["complete_success"]) for r in eligible)
    passed = sum(bool(r["evaluation"] and r["evaluation"]["contract_pass"]) for r in eligible)
    walls = sorted(r["metadata"]["wall_seconds"] for r in ability
                   if r["status"] not in ("pending", "running") and r["metadata"].get("started_at")
                   and r["metadata"].get("wall_seconds") is not None)
    # v1 records have a duration but no started_at; zero defaults are never inferred.
    walls.extend(r["metadata"]["wall_seconds"] for r in ability
                 if r["status"] in NATURAL and not r["metadata"].get("started_at")
                 and "wall_seconds" in r["metadata"])
    walls.sort()
    def percentile(p):
        return walls[max(0, math.ceil(len(walls) * p) - 1)] if walls else None
    def count(name, fallback=0):
        values = []
        for run in eligible:
            ev = run["evaluation"]
            if ev and ev["version"] == "facts-and-citations-v1":
                values.append(ev.get({"field_groups_correct":"facts_correct", "field_groups_total":"facts_total"}.get(name, ""), 0))
            elif ev:
                values.append(ev.get(name, fallback))
            else:
                key = {"facts_total":"facts", "facts_missing":"facts", "citations_total":"citations", "field_groups_total":"field_groups"}.get(name, "")
                values.append(run["metadata"].get("expected_counts", {}).get(key, fallback))
        return sum(values)
    rules = sum(len(r["metadata"]["fault_ids"]) for r in fault_runs)
    activated_rules = sum(len(set(r["metadata"]["fault_ids"]) & set(r["metadata"].get("activated_faults", []))) for r in fault_runs)
    executed = sum(bool(r["metadata"].get("started_at") or r["metadata"].get("wall_seconds") is not None) for r in ability)
    invalid = sum(r.get("integrity", {}).get("state") == "invalid" for r in ability)
    unverified = sum(r.get("integrity", {}).get("state") == "unverified" and r["status"] in NATURAL for r in ability)
    completed_coverage = ratio(len(eligible), len(ability))
    model_runs = [r for r in ability if r["metadata"].get("agent_kind") == "model"]
    model_attempted = sum(r["metadata"].get("model_calls", 0) > 0 for r in model_runs)
    return {"total": len(runs), "eligible": len(eligible), "evaluated": len(evaluated),
            "executed": executed, "execution_coverage": ratio(executed, len(ability)),
            "completion_coverage": completed_coverage,
            "experiment_complete": bool(ability) and len(eligible) == len(ability) and model_attempted == len(model_runs),
            "model_attempted_runs": model_attempted, "model_execution_coverage": ratio(model_attempted, len(model_runs)),
            "integrity_invalid": invalid, "integrity_unverified": unverified,
            "fact_denominator_unknown": sum(bool(r["evaluation"] and r["evaluation"]["version"] == "facts-and-citations-v1") or
                                            (not r["evaluation"] and not r["metadata"].get("expected_counts")) for r in eligible),
            "complete": complete, "contract_pass": passed,
            "complete_rate": ratio(complete, len(eligible)), "contract_rate": ratio(passed, len(eligible)),
            "recoverable_activated": len(recoverable),
            "recovered": sum(bool(r["evaluation"] and r["evaluation"]["complete_success"]) for r in recoverable),
            "recovery_rate": ratio(sum(bool(r["evaluation"] and r["evaluation"]["complete_success"]) for r in recoverable), len(recoverable)),
            "fault_runs": len(fault_runs), "activated": len(activated), "fault_coverage": ratio(len(activated), len(fault_runs)),
            "fault_rules": rules, "activated_rules": activated_rules, "rule_coverage": ratio(activated_rules, rules),
            "tool_calls": sum(r["metadata"].get("tool_calls", 0) for r in ability),
            "model_calls": sum(r["metadata"].get("model_calls", 0) for r in ability),
            "input_tokens": sum(r["metadata"].get("input_tokens", 0) for r in ability),
            "output_tokens": sum(r["metadata"].get("output_tokens", 0) for r in ability),
            "usage_unknown_calls": sum(r["metadata"].get("usage_unknown_calls", 0) for r in ability),
            "retries": sum(r["metadata"].get("retries", 0) for r in ability),
            "p50_seconds": percentile(.5), "p95_seconds": percentile(.95), "timed_runs": len(walls),
            "simulated_fault_seconds": sum(r["metadata"].get("simulated_fault_seconds", 0) for r in ability),
            **{name: count(name) for name in ("facts_correct", "facts_total", "facts_missing", "facts_errors", "facts_reported",
                                            "field_groups_correct", "field_groups_total", "citations_valid", "citations_total")},
            "cost": 0 if all(r["metadata"].get("agent_kind") == "script" for r in ability) and ability else None,
            "status_counts": dict(Counter(r["status"] for r in runs)),
            "recovery_attribution": "Gateway rejects duplicate/stale envelopes; agent chooses retries and backup reads."}


def compare(left, right):
    reasons = []
    fields = ("suite_hash", "budget", "mode", "case_ids", "seeds", "agent_kind", "comparison_axis",
              "framework_fingerprint", "tool_fingerprint", "runtime")
    if left["config"].get("agent_kind") == "model" or right["config"].get("agent_kind") == "model":
        fields += ("provider", "model", "model_config", "transport_version", "prompt_version")
    for field in fields:
        if left["config"].get(field) != right["config"].get(field):
            reasons.append(field + " differs")
    if not left["config"].get("framework_fingerprint") or not right["config"].get("framework_fingerprint"):
        reasons.append("framework provenance unavailable")
    for index, experiment in enumerate((left, right)):
        if experiment["config"]["mode"] == "trace_replay":
            reasons.append("trace_replay is excluded from ability comparison")
        if any(r["status"] not in NATURAL for r in experiment["runs"]):
            reasons.append("experiment contains unfinished, cancelled or interrupted runs")
        if any(r.get("integrity", {}).get("state") != "valid" for r in experiment["runs"]):
            reasons.append("run integrity is not verified")
        model_runs = [r for r in experiment["runs"] if r["metadata"].get("agent_kind") == "model"]
        if model_runs:
            if any(r["metadata"].get("model_calls", 0) <= 0 for r in model_runs):
                reasons.append("model execution coverage incomplete")
            reported = {model for r in model_runs for model in r["metadata"].get("reported_models", [])}
            if len(reported) != 1 or any(not r["metadata"].get("reported_models") for r in model_runs):
                reasons.append("reported model provenance incomplete or mixed")
        versions = {(r["evaluation"]["version"], r["evaluation"].get("evaluator_fingerprint")) if r["evaluation"] else
                    (r["metadata"].get("evaluator_version", experiment["config"].get("evaluator_version")), r["metadata"].get("evaluator_fingerprint"))
                    for r in experiment["runs"]}
        experiment_version = next(iter(versions)) if len(versions) == 1 else None
        if index == 0:
            left_version = experiment_version
        else:
            right_version = experiment_version
        expected = {(case_id, seed) for case_id in experiment["config"]["case_ids"] for seed in experiment["config"]["seeds"]}
        actual = [(r["case_id"], r["metadata"]["seed"]) for r in experiment["runs"]]
        if len(actual) != len(set(actual)):
            reasons.append("duplicate run pairs")
        if set(actual) != expected:
            reasons.append("planned run coverage differs")
    if left_version is None or left_version != right_version:
        reasons.append("evaluator_version differs")
    reported_left = {m for r in left["runs"] for m in r["metadata"].get("reported_models", [])}
    reported_right = {m for r in right["runs"] for m in r["metadata"].get("reported_models", [])}
    if reported_left != reported_right:
        reasons.append("reported model differs")
    a = {(r["case_id"], r["metadata"]["seed"]): r for r in left["runs"]}
    b = {(r["case_id"], r["metadata"]["seed"]): r for r in right["runs"]}
    if a.keys() != b.keys():
        reasons.append("run pairs differ")
    reasons = list(dict.fromkeys(reasons))
    rows = []
    for key in sorted(a.keys() & b.keys()):
        left_pass = bool(a[key]["evaluation"] and a[key]["evaluation"]["contract_pass"])
        right_pass = bool(b[key]["evaluation"] and b[key]["evaluation"]["contract_pass"])
        rows.append({"case_id": key[0], "seed": key[1], "left_run": a[key]["id"], "right_run": b[key]["id"],
                     "left_pass": left_pass, "right_pass": right_pass,
                     "change": None if reasons else "improved" if right_pass and not left_pass else "regressed" if left_pass and not right_pass else "same"})
    x, y = summary(left["runs"]), summary(right["runs"])
    delta = y["contract_rate"] - x["contract_rate"] if not reasons and a and x["contract_rate"] is not None and y["contract_rate"] is not None else None
    return {"comparable": not reasons, "reasons": reasons, "left": x, "right": y, "pairs": rows, "contract_rate_delta": delta}

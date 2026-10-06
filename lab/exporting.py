import csv
import io


def csv_export(result):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    columns = ["case_id", "run_id", "seed", "strategy", "mode", "status", "integrity",
               "integrity_reason", "evaluation_version", "contract_pass", "complete_success",
               "historical_contract_pass", "tool_calls", "model_calls", "facts_correct", "facts_total",
               "facts_missing", "facts_errors", "citations_valid", "citations_total", "agent_kind", "provider", "model",
               "prompt_version", "input_tokens", "output_tokens", "usage_unknown_calls", "reserved_input_tokens", "reserved_output_tokens"]
    columns += ["executed_tool_calls", "executed_model_calls"]
    writer.writerow(columns)
    for run in result["runs"]:
        meta, ev, history = run["metadata"], run["evaluation"] or {}, run.get("historical_evaluation") or {}
        values = dict(meta, case_id=run["case_id"], run_id=run["id"], status=run["status"],
                      integrity=run["integrity"]["state"], integrity_reason=run["integrity"].get("reason"),
                      evaluation_version=ev.get("version"), historical_contract_pass=history.get("contract_pass"), **ev)
        writer.writerow([values.get(key) for key in columns])
    return "\ufeff" + output.getvalue()

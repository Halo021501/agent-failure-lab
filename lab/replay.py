import copy
from .common import digest, LabError


def recorded_calls(events):
    result, current = [], None
    for event in events:
        if event["kind"] == "tool_call":
            if current is not None:
                result.append(current)
            call = event["payload"]
            current = {"tool": call["tool"], "args": call["args"], "events": []}
        elif current is not None and event["kind"] in ("fault_activated", "tool_raw_result", "tool_delivery",
                                                     "tool_result", "tool_error", "artifact_committed"):
            if event["kind"] == "artifact_committed" and current["tool"] != "write_report":
                raise LabError("replay_incomplete", "提交事件没有匹配写入调用")
            current["events"].append(copy.deepcopy(event))
            if event["kind"] == "tool_result":
                current["result"] = event["payload"]["result"]
            elif event["kind"] == "tool_error":
                current["error"] = event["payload"]
    if current is not None:
        result.append(current)
    if any("result" not in call and "error" not in call for call in result):
        raise LabError("replay_incomplete", "录制调用没有终态结果")
    return result


def normalized_events(events):
    return [{"seq": e["seq"], "kind": e["kind"], "payload": e["payload"]} for e in events]


def trace_replay(store, source_id, experiment_id, run_id=None):
    source = store.run(source_id, force=True)
    if source["integrity"]["state"] != "valid":
        raise LabError(source["integrity"]["reason"], "原记录完整性校验失败")
    events = store.events(source_id)
    report = store.optional_report(source_id)
    metadata = dict(source["metadata"], mode="trace_replay", parent_run_id=source_id,
                    executed_tool_calls=0, executed_model_calls=0,
                    original_wall_seconds=source["metadata"].get("wall_seconds"),
                    events_sha256=digest(normalized_events(events)),
                    trace_sha256=digest(normalized_events(events)))
    run_id = run_id or store.create_run(experiment_id, source["case_id"], metadata)
    if report is not None:
        writes = [e["payload"]["args"] for e in events if e["kind"] == "tool_call" and e["payload"]["tool"] == "write_report"]
        key = writes[0]["idempotency_key"] if writes else "release-report-v1"
        store.save_report(run_id, report, key)
    with store.lock, store.db:
        store.db.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        for event in events:
            store._event(run_id, event["kind"], event["payload"])
    store.finish(run_id, source["status"], source["historical_evaluation"], metadata)
    return run_id

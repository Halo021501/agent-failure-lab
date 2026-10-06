import copy
import time
from collections import defaultdict
from .common import canonical, digest, Halt, LabError
from .faults import FaultInjector
from .tools import Tools


class Gateway:
    def __init__(self, case, store, run_id, budget, cancel, seed=0, replay_calls=None, on_call=None):
        self.case, self.store, self.run_id, self.budget, self.cancel = case, store, run_id, budget, cancel
        self.tools = Tools(case, store, run_id)
        self.faults = FaultInjector(case, seed)
        self.started = time.perf_counter()
        self.tool_calls = 0
        self.simulated_seconds = 0
        self.counts = defaultdict(int)
        self.last_envelope = None
        self.replay_calls = replay_calls
        self.replay_position = 0
        self.on_call = on_call

    def event(self, kind, data):
        self.store.event(self.run_id, kind, data)

    def check(self):
        if self.cancel.is_set():
            raise Halt("cancelled", "Run cancelled")
        if time.perf_counter() - self.started > self.budget.wall_seconds:
            raise Halt("budget_exceeded", "Wall clock budget exceeded")

    def call(self, tool, args):
        self.check()
        if self.tool_calls >= self.budget.max_tool_calls:
            raise Halt("budget_exceeded", "Tool call budget exceeded")
        self.tool_calls += 1
        call_id = f"call-{self.tool_calls:03d}"
        key = digest({"tool": tool, "args": args})
        self.counts[key] += 1
        self.event("tool_call", {"call_id": call_id, "tool": tool, "args": args,
                                "args_hash": digest(args), "logical_operation_id": key,
                                "attempt": self.counts[key]})
        if self.on_call:
            self.on_call(self)
        self.check()
        if self.replay_calls is not None:
            return self._replay(tool, args, call_id)
        try:
            rules = self.faults.matching(tool, args)
            self.faults.before(rules, self)
            tool_started = time.perf_counter()
            raw = self.tools.execute(tool, args)
            self.event("tool_raw_result", {"call_id": call_id, "result": raw})
            if time.perf_counter() - tool_started > self.budget.tool_timeout_seconds:
                raise LabError("timeout", "Local tool exceeded its deadline")
            result = self.faults.after(rules, raw, self)
            envelope = {"call_id": call_id, "result": result}
            deliveries = [envelope]
            for rule in rules:
                if rule["phase"] == "delivery":
                    self.faults.mark(rule, self)
                    if rule["action"] == "duplicate_delivery":
                        deliveries.append(copy.deepcopy(envelope))
                    elif rule["action"] == "stale_delivery" and self.last_envelope:
                        deliveries.insert(0, copy.deepcopy(self.last_envelope))
            accepted = False
            for delivered in deliveries:
                decision = "accepted"
                if delivered["call_id"] != call_id:
                    decision = "stale_rejected"
                elif accepted:
                    decision = "duplicate_rejected"
                else:
                    accepted = True
                self.event("tool_delivery", {"expected_call_id": call_id, "envelope": delivered, "decision": decision})
            self.check()
            self.event("tool_result", envelope)
            self.last_envelope = copy.deepcopy(envelope)
            return result
        except Halt:
            raise
        except LabError as error:
            self.event("tool_error", {"call_id": call_id, "code": error.code, "message": str(error)})
            raise

    def _replay(self, tool, args, call_id):
        if self.replay_position >= len(self.replay_calls):
            raise Halt("replay_mismatch", "Recording has no matching next call")
        recorded = self.replay_calls[self.replay_position]
        if recorded["tool"] != tool or canonical(recorded["args"]) != canonical(args):
            raise Halt("replay_mismatch", "Next recorded tool/arguments do not match")
        self.replay_position += 1
        for event in recorded["events"]:
            if event["kind"] == "artifact_committed":
                if tool != "write_report" or event["payload"]["sha256"] != digest(args["report"]):
                    raise Halt("replay_mismatch", "录制的提交与写入内容不一致")
                self.store.save_report(self.run_id, args["report"], args["idempotency_key"])
                continue
            if event["kind"] == "fault_activated":
                self.faults.activated.add(event["payload"]["fault_id"])
                if event["payload"]["action"] in ("timeout", "lost_ack"):
                    self.simulated_seconds += self.budget.tool_timeout_seconds
            payload = copy.deepcopy(event["payload"])
            if "call_id" in payload:
                payload["call_id"] = call_id
            self.event(event["kind"], payload)
        if recorded.get("error"):
            raise LabError(recorded["error"]["code"], recorded["error"]["message"])
        result = copy.deepcopy(recorded["result"])
        return result

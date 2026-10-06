import copy
import json
from collections import defaultdict
from .common import canonical, content_hash, LabError


class FaultInjector:
    def __init__(self, case, seed=0):
        self.case, self.seed = case, seed
        self.counts = defaultdict(int)
        self.activated = set()

    def matching(self, tool, args):
        rules = []
        source = self.case.source(args.get("source_id"))
        context = dict(args)
        if source:
            context.update(group=source["group"], scope=source["scope"])
        for rule in self.case.definition["faults"]:
            if rule["tool"] != tool or any(context.get(k) != v for k, v in rule.get("selector", {}).items()):
                continue
            key = (rule["id"], canonical(args))
            self.counts[key] += 1
            if rule["occurrences"] == "always" or self.counts[key] in rule["occurrences"]:
                rules.append(rule)
        return rules

    def mark(self, rule, gateway):
        self.activated.add(rule["id"])
        gateway.event("fault_activated", {"fault_id": rule["id"], "action": rule["action"], "phase": rule["phase"]})

    def before(self, rules, gateway):
        for rule in rules:
            if rule["phase"] == "before":
                self.mark(rule, gateway)
                if rule["action"] == "timeout":
                    gateway.simulated_seconds += gateway.budget.tool_timeout_seconds
                raise LabError(rule["action"], "Injected " + rule["action"])

    def after(self, rules, raw, gateway):
        result = copy.deepcopy(raw)
        for rule in rules:
            if rule["phase"] != "after":
                continue
            self.mark(rule, gateway)
            action = rule["action"]
            if action == "lost_ack":
                gateway.simulated_seconds += gateway.budget.tool_timeout_seconds
                raise LabError("timeout", "Committed report acknowledgement lost")
            if action == "empty":
                if "content" in result:
                    result["content"] = ""
                else:
                    result["sources"] = []
            elif action == "stale_index":
                result["sources"] = [dict(s, id="obsolete-release") if s["group"] == "release" else s for s in result["sources"]]
            elif action == "truncate":
                result["content"] = result["content"][:10]
            elif action == "wrong_type":
                result["content"] = json.dumps({"version": self.case.definition["target_version"], "issues": "invalid"}, ensure_ascii=False)
            elif action == "bad_csv":
                result["content"] = "version,test_id,status\n1.4.0,T01,maybe\n"
            elif action == "duplicate_items":
                data = json.loads(result["content"])
                data["issues"] += copy.deepcopy(data["issues"][:1])
                result["content"] = json.dumps(data, ensure_ascii=False)
        if "content" in result:
            result["content_sha256"] = content_hash(result["content"])
        return result

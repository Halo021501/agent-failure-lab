import csv
import io
import json
from ..common import LabError

STRATEGIES = ("recovery", "no-recovery")


def reference(source, locator):
    return {"source_id": source["source_id"], "content_sha256": source["content_sha256"], "locator": locator}


def parse_source(source, target):
    text, group = source["content"], source["group"]
    if not text.strip():
        raise LabError("empty", "Source content is empty")
    if group == "notes":
        return {}
    try:
        if group == "release":
            lines = text.splitlines()
            versions = [(i + 1, line[9:]) for i, line in enumerate(lines) if line.startswith("version: ")]
            if len(versions) != 1:
                raise ValueError("Release requires one version field")
            if versions[0][1] != target:
                return {"other_version": True}
            features = [{"text": line[9:], "evidence": [reference(source, {"kind": "line", "line": i + 1})]}
                        for i, line in enumerate(lines) if line.startswith("feature: ")]
            return {"version": target, "version_evidence": [reference(source, {"kind": "line", "line": versions[0][0]})], "features": features}
        if group == "issues":
            data = json.loads(text)
            if not isinstance(data, dict) or not isinstance(data.get("issues"), list) or not isinstance(data.get("version"), str):
                raise ValueError("Invalid issue structure")
            if data["version"] != target:
                return {"other_version": True}
            issues = {}
            for index, issue in enumerate(data["issues"]):
                if not isinstance(issue, dict) or any(not isinstance(issue.get(k), str) for k in ("id", "title", "status")) or issue["status"] not in ("open", "closed"):
                    raise ValueError("Invalid issue fields")
                if issue["status"] == "open":
                    issues.setdefault(issue["id"], dict(issue, evidence=[reference(source, {"kind": "json", "pointer": f"/issues/{index}"})]))
            return {"issues": list(issues.values())}
        if group == "tests":
            reader = csv.DictReader(io.StringIO(text))
            if reader.fieldnames != ["version", "test_id", "status"]:
                raise ValueError("Invalid CSV columns")
            tests = {}
            for index, row in enumerate(reader):
                if None in row or any(not row[k] for k in reader.fieldnames) or row["status"] not in ("passed", "failed"):
                    raise ValueError("Invalid test row")
                if row["version"] == target:
                    tests.setdefault(row["test_id"], {"status": row["status"], "evidence": reference(source, {"kind": "csv", "row": index + 2})})
            if not tests:
                raise ValueError("Required test data is absent")
            return {"tests": tests}
    except (ValueError, TypeError, KeyError, csv.Error) as error:
        raise LabError("format_error", str(error)) from error
    raise LabError("format_error", "Unknown document group")


class Baseline:
    version = "baseline-v1"

    def __init__(self, strategy, budget):
        if strategy not in STRATEGIES:
            raise ValueError("Unknown offline strategy")
        self.strategy, self.budget = strategy, budget
        self.retries = budget.max_read_attempts if strategy == "recovery" else 1

    def execute(self, gateway, target):
        failures = {}
        attempts = {}

        def read(tool, args, parser, group):
            attempts.setdefault(group, 0)
            for _ in range(self.retries):
                attempts[group] += 1
                try:
                    result = gateway.call(tool, args)
                    try:
                        return parser(result)
                    except LabError as error:
                        gateway.event("parse_error", {"group": group, "code": error.code, "source_id": args.get("source_id")})
                        raise
                except LabError as error:
                    # Termination exceptions must never be converted to a partial report.
                    from ..common import Halt
                    if isinstance(error, Halt):
                        raise
                    failures[group] = error.code
                    if error.code in ("missing", "invalid_arguments", "idempotency_conflict"):
                        break
            return None

        def parse_index(result):
            if not isinstance(result.get("sources"), list) or not result["sources"]:
                raise LabError("empty", "Empty source index")
            return result["sources"]

        primary = read("list_sources", {"scope": "primary"}, parse_index, "index") or []
        collected = {"release": [], "issues": [], "tests": []}

        def consume(index, needed):
            for source in index:
                group = source["group"]
                if group not in needed and group != "notes":
                    continue
                parsed = read("read_source", {"source_id": source["id"]},
                              lambda value: parse_source(value, target), group)
                if parsed is not None and not parsed.get("other_version") and group in collected:
                    collected[group].append(parsed)

        consume(primary, set(collected))
        missing = {g for g, values in collected.items() if not values}
        if missing and self.strategy == "recovery":
            backup = read("list_sources", {"scope": "backup"}, parse_index, "index") or []
            consume(backup, missing)
        missing = {g for g, values in collected.items() if not values}
        releases = collected["release"]
        features, issues, tests = {}, {}, {}
        for value in releases:
            for item in value["features"]:
                features.setdefault(item["text"], item)
        for value in collected["issues"]:
            for item in value["issues"]:
                issues.setdefault(item["id"], item)
        for value in collected["tests"]:
            tests.update(value["tests"])
        gaps = [{"group": group, "reason": failures.get(group, failures.get("index", "unavailable")), "attempts": attempts.get(group, attempts.get("index", 1))}
                for group in sorted(missing)]
        report = {"schema_version": 1, "release_version": target if releases else None,
                  "release_evidence": [ref for value in releases for ref in value["version_evidence"]],
                  "features": list(features.values()) if releases else None,
                  "unresolved_issues": list(issues.values()) if collected["issues"] else None,
                  "test_summary": {"total": len(tests), "passed": sum(t["status"] == "passed" for t in tests.values()),
                                   "failed": sum(t["status"] == "failed" for t in tests.values()),
                                   "evidence": [t["evidence"] for t in tests.values()]} if tests else None,
                  "gaps": gaps, "overall_status": "partial" if missing else "complete"}
        gateway.check()
        for _ in range(self.retries):
            try:
                gateway.call("write_report", {"report": report, "idempotency_key": "release-report-v1"})
                return report
            except LabError as error:
                from ..common import Halt
                if isinstance(error, Halt) or error.code != "timeout":
                    raise
        raise LabError("timeout", "Report acknowledgement unavailable")

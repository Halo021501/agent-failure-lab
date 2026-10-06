import csv
import io
import json
from collections import Counter
from .common import content_hash
from .common import canonical
from .schemas import validate_report

VERSION = "facts-and-citations-v3"


def fact_atoms(value, report=False):
    atoms = []
    if value.get("release_version") is not None:
        atoms.append(("version", value["release_version"]))
    features = value.get("features")
    if features is not None:
        atoms.extend(("feature", item["text"] if report else item) for item in features)
        if not features:
            atoms.append(("empty_features", True))
    issues = value.get("unresolved_issues")
    if issues is not None:
        atoms.extend(("issue", canonical({k: item[k] for k in ("id", "title", "status")})) for item in issues)
        if not issues:
            atoms.append(("empty_issues", True))
    tests = value.get("test_summary")
    if tests is not None:
        atoms.extend(("tests_" + key, tests[key]) for key in ("total", "passed", "failed"))
    return Counter(atoms)


def expected_counts(expected):
    citations = (expected["release_version"] is not None) + len(expected["features"] or [])
    citations += max(1, len(expected["unresolved_issues"])) if expected["unresolved_issues"] is not None else 0
    citations += len(expected["test_rows"]) if expected["test_summary"] is not None else 0
    return {"facts": sum(fact_atoms(expected).values()), "citations": citations, "field_groups": 4}


def evaluate(case, report, events):
    checks = []

    def check(name, passed, detail=""):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    try:
        validate_report(report)
    except (ValueError, TypeError, KeyError) as error:
        check("report_schema", False, str(error))
        return {"version": VERSION, "contract_pass": False, "complete_success": False, "checks": checks,
                "facts_correct": 0, "facts_total": expected_counts(case.expected)["facts"],
                "facts_missing": expected_counts(case.expected)["facts"], "facts_errors": 0,
                "facts_reported": 0, "field_groups_correct": 0, "field_groups_total": 4,
                "citations_valid": 0, "citations_total": expected_counts(case.expected)["citations"]}
    check("report_schema", True)
    expected = case.expected
    expected_features = expected["features"]
    actual_features = None if report["features"] is None else sorted(i["text"] for i in report["features"])
    expected_issues = expected["unresolved_issues"]
    actual_issues = None if report["unresolved_issues"] is None else sorted(
        [{k: i[k] for k in ("id", "title", "status")} for i in report["unresolved_issues"]], key=lambda i: i["id"])
    facts = [
        ("version_fact", report["release_version"] == expected["release_version"]),
        ("feature_facts", actual_features == (None if expected_features is None else sorted(expected_features))),
        ("issue_facts", actual_issues == (None if expected_issues is None else sorted(expected_issues, key=lambda i: i["id"]))),
        ("test_facts", (None if report["test_summary"] is None else {k: report["test_summary"][k] for k in ("total", "passed", "failed")}) == expected["test_summary"])
    ]
    for name, passed in facts:
        check(name, passed)
    check("expected_status", report["overall_status"] == expected["overall_status"])
    gaps = report["gaps"]
    check("required_gaps", sorted(g["group"] for g in gaps) == sorted(expected["missing_groups"]))
    check("partial_is_explicit", (report["overall_status"] == "partial") == bool(gaps))
    calls = {e["payload"]["call_id"]: e["payload"] for e in events if e["kind"] == "tool_call"}
    reads = {}
    for event in events:
        if event["kind"] == "tool_result":
            result = event["payload"].get("result", {})
            if "content" in result:
                reads[(result["source_id"], result["content_sha256"])] = result
    for gap in gaps:
        source_ids = {s["id"] for s in case.definition["sources"] if s["group"] == gap["group"]}
        attempts = sum(c["tool"] == "read_source" and isinstance(c["args"].get("source_id"), str)
                       and c["args"].get("source_id") in source_ids for c in calls.values())
        error_exists = any(e["kind"] == "parse_error" and e["payload"].get("group") == gap["group"] or
                           e["kind"] == "tool_error" and isinstance(calls.get(e["payload"]["call_id"], {}).get("args", {}).get("source_id"), str)
                           and calls.get(e["payload"]["call_id"], {}).get("args", {}).get("source_id") in source_ids
                           for e in events)
        # Empty indexes can prevent any source read.
        index_errors = any(e["kind"] == "parse_error" and e["payload"].get("group") == "index" for e in events)
        # Evaluation inspects observed public content; it does not supply parser results to the model.
        if not error_exists:
            from .agents.baseline import parse_source
            for source in reads.values():
                if source.get("group") != gap["group"]:
                    continue
                try:
                    parse_source(source, case.definition["target_version"])
                except Exception as error:
                    from .common import LabError
                    if not isinstance(error, LabError):
                        raise
                    error_exists = True
        index_errors |= any(e["kind"] == "tool_result" and e["payload"].get("result", {}).get("sources") == [] for e in events)
        check("gap_" + gap["group"], (error_exists or index_errors) and gap["attempts"] >= 1 and (attempts == gap["attempts"] if attempts else index_errors))
    citation_results = []
    citation_credit = 0
    credited = set()

    def resolve(ref):
        source = reads.get((ref["source_id"], ref["content_sha256"]))
        if not source or content_hash(source["content"]) != ref["content_sha256"]:
            return None
        location = ref["locator"]
        try:
            kind = location["kind"]
            if kind == "line" and source["format"] == "markdown" and set(location) == {"kind", "line"} and type(location["line"]) is int and location["line"] >= 1:
                return source["content"].splitlines()[location["line"] - 1]
            if kind == "json" and source["format"] == "json" and set(location) == {"kind", "pointer"}:
                parts = location["pointer"].split("/")
                if len(parts) != 3 or parts[0] or parts[1] != "issues" or not parts[2].isdigit():
                    return None
                value = json.loads(source["content"])
                if value["version"] != case.definition["target_version"]:
                    return None
                return value["issues"][int(parts[2])]
            if kind == "csv" and source["format"] == "csv" and set(location) == {"kind", "row"} and type(location["row"]) is int and location["row"] >= 2:
                return list(csv.DictReader(io.StringIO(source["content"])))[location["row"] - 2]
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        return None

    def citation(name, refs, predicate, credit_key=None):
        nonlocal citation_credit
        valid = bool(refs) and all(predicate(resolve(ref)) for ref in refs)
        citation_results.append(valid)
        if valid and credit_key is not None and credit_key not in credited:
            credited.add(credit_key)
            citation_credit += 1
        check(name, valid)

    if report["release_version"] is not None:
        citation("version_citation", report["release_evidence"], lambda v: v == "version: " + report["release_version"],
                 ("version", report["release_version"]) if report["release_version"] == expected["release_version"] else None)
    elif report["release_evidence"]:
        check("unknown_version_has_no_citations", False)
    for index, feature in enumerate(report["features"] or []):
        citation(f"feature_citation_{index}", feature["evidence"], lambda v, f=feature: v == "feature: " + f["text"],
                 ("feature", feature["text"]) if feature["text"] in (expected["features"] or []) else None)
    if report["unresolved_issues"] is not None:
        if not report["unresolved_issues"]:
            # Empty lists still require a successfully read target-version issue document.
            valid_empty = False
            for source in reads.values():
                if source["group"] != "issues":
                    continue
                try:
                    value = json.loads(source["content"])
                    valid_empty |= value["version"] == case.definition["target_version"] and isinstance(value["issues"], list) and not any(i["status"] == "open" for i in value["issues"])
                except (ValueError, KeyError, TypeError):
                    pass
            citation_results.append(valid_empty)
            citation_credit += valid_empty and expected["unresolved_issues"] == []
            check("empty_issues_supported", valid_empty)
        for index, issue in enumerate(report["unresolved_issues"]):
            issue_value = {k: issue[k] for k in ("id", "title", "status")}
            citation(f"issue_citation_{index}", issue["evidence"],
                     lambda v, i=issue: isinstance(v, dict) and all(v.get(k) == i[k] for k in ("id", "title", "status")),
                     ("issue", canonical(issue_value)) if issue_value in (expected["unresolved_issues"] or []) else None)
    if report["test_summary"] is not None:
        refs = report["test_summary"]["evidence"]
        resolved = [resolve(ref) for ref in refs]
        normalized = sorted(resolved, key=lambda r: r.get("test_id", "")) if all(isinstance(r, dict) for r in resolved) else []
        expected_rows = sorted(expected["test_rows"], key=lambda r: r["test_id"])
        valid = bool(refs) and normalized == expected_rows
        citation_results.append(valid)
        expected_row_counts = Counter(canonical(r) for r in expected_rows)
        resolved_row_counts = Counter(canonical(r) for r in resolved if isinstance(r, dict))
        citation_credit += sum((expected_row_counts & resolved_row_counts).values())
        check("test_citations_cover_expected_rows", valid)
    # Missing citations must not disappear from the denominator because the output omitted facts.
    expected_citation_count = expected_counts(expected)["citations"]
    check("single_artifact_commit", sum(e["kind"] == "artifact_committed" for e in events) == 1)
    writes = [c["args"] for c in calls.values() if c["tool"] == "write_report"]
    check("consistent_write_key", bool(writes) and all(isinstance(w.get("idempotency_key"), str) for w in writes)
          and len({w.get("idempotency_key") for w in writes if isinstance(w.get("idempotency_key"), str)}) == 1)
    passed = all(c["passed"] for c in checks)
    expected_atoms, actual_atoms = fact_atoms(expected), fact_atoms(report, report=True)
    correct = sum((expected_atoms & actual_atoms).values())
    return {"version": VERSION, "contract_pass": passed,
            "complete_success": passed and report["overall_status"] == "complete",
            "checks": checks, "facts_correct": correct, "facts_total": sum(expected_atoms.values()),
            "facts_missing": sum((expected_atoms - actual_atoms).values()),
            "facts_errors": sum((actual_atoms - expected_atoms).values()), "facts_reported": sum(actual_atoms.values()),
            "field_groups_correct": sum(p for _, p in facts), "field_groups_total": len(facts),
            "citations_valid": citation_credit, "citations_total": expected_citation_count}

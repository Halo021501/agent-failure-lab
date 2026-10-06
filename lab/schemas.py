from dataclasses import asdict, dataclass
import json
import math
from .common import ROOT


@dataclass(frozen=True)
class Budget:
    tool_timeout_seconds: float = 15
    wall_seconds: float = 90
    max_tool_calls: int = 16
    max_model_calls: int = 12
    max_read_attempts: int = 3

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid budget: {name}")
        for name in ("max_tool_calls", "max_model_calls", "max_read_attempts"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"Budget must be an integer: {name}")
        if self.max_tool_calls > 256 or self.max_model_calls > 256 or self.max_read_attempts > 10 or self.wall_seconds > 3600:
            raise ValueError("Budget exceeds local experiment limits")

    def to_dict(self):
        return asdict(self)


def validate_report(report):
    if not isinstance(report, dict):
        raise ValueError("Report must be an object")
    required = {"schema_version", "release_version", "release_evidence", "features",
                "unresolved_issues", "test_summary", "gaps", "overall_status"}
    if set(report) != required or type(report["schema_version"]) is not int or report["schema_version"] != 1:
        raise ValueError("Report fields/schema do not match v1")
    if report["overall_status"] not in ("complete", "partial"):
        raise ValueError("Invalid report status")
    if report["release_version"] is not None and not isinstance(report["release_version"], str):
        raise ValueError("Invalid release version")
    if not isinstance(report["release_evidence"], list) or not isinstance(report["gaps"], list):
        raise ValueError("Invalid report evidence/gaps")
    for field in ("features", "unresolved_issues"):
        if report[field] is not None and not isinstance(report[field], list):
            raise ValueError(f"Invalid {field}")
    for item in report["features"] or []:
        if set(item) != {"text", "evidence"} or not isinstance(item["text"], str) or not isinstance(item["evidence"], list):
            raise ValueError("Invalid feature")
    for item in report["unresolved_issues"] or []:
        if set(item) != {"id", "title", "status", "evidence"} or not all(isinstance(item[k], str) for k in ("id", "title", "status")) or not isinstance(item["evidence"], list):
            raise ValueError("Invalid issue")
    summary = report["test_summary"]
    if summary is not None:
        if set(summary) != {"total", "passed", "failed", "evidence"} or not isinstance(summary["evidence"], list):
            raise ValueError("Invalid test summary")
        if any(type(summary[k]) is not int or summary[k] < 0 for k in ("total", "passed", "failed")):
            raise ValueError("Test counts must be nonnegative integers")
        if summary["total"] != summary["passed"] + summary["failed"]:
            raise ValueError("Test counts do not sum")
    for gap in report["gaps"]:
        if set(gap) != {"group", "reason", "attempts"} or not isinstance(gap["group"], str) or not isinstance(gap["reason"], str) or type(gap["attempts"]) is not int or gap["attempts"] < 1:
            raise ValueError("Invalid gap")
    evidence = list(report["release_evidence"])
    for item in (report["features"] or []) + (report["unresolved_issues"] or []):
        evidence.extend(item["evidence"])
    if summary:
        evidence.extend(summary["evidence"])
    for ref in evidence:
        if not isinstance(ref, dict) or set(ref) != {"source_id", "content_sha256", "locator"}:
            raise ValueError("Invalid citation")
        if not isinstance(ref["source_id"], str) or not isinstance(ref["content_sha256"], str) or len(ref["content_sha256"]) != 64 or not isinstance(ref["locator"], dict):
            raise ValueError("Invalid citation fields")


def configured_budget(overrides=None):
    config = json.loads((ROOT / "config/defaults.json").read_text(encoding="utf-8"))
    if type(config.get("schema_version")) is not int or config["schema_version"] != 1:
        raise ValueError("Invalid configuration schema")
    fields = {"tool_timeout_seconds", "wall_seconds", "max_tool_calls", "max_model_calls", "max_read_attempts"}
    if overrides is not None and (not isinstance(overrides, dict) or set(overrides) - fields):
        raise ValueError("Unknown budget configuration fields")
    values = {name: config[name] for name in fields}
    values.update(overrides or {})
    return Budget(**values)

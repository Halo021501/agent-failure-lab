"""Build synthetic public documents and separately stored expected facts."""
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab.common import canonical


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def rule(case_id, action, tool="read_source", selector=None, occurrences="always", phase=None):
    phase = phase or ("before" if action in ("timeout", "missing") else
                      "delivery" if action in ("duplicate_delivery", "stale_delivery") else "after")
    return {"id": case_id + "-" + action, "action": action, "tool": tool, "selector": selector or {},
            "occurrences": occurrences, "phase": phase}


def build():
    descriptions = {
        "N01": "完整的小规模发布资料", "N02": "中英文混合资料", "N03": "功能分散在两个文件",
        "N04": "目标版本与旧版本混合", "N05": "合法空问题列表", "N06": "问题去重与已解决筛选",
        "F-T01": "首次读取超时", "F-T02": "前两次读取超时", "F-T03": "主资料超时，备用可读",
        "F-T04": "所有测试资料持续超时", "F-M01": "主资料缺失，备用可读",
        "F-M02": "非必需说明缺失", "F-M03": "主索引指向过期文件",
        "F-M04": "所有必需资料缺失", "F-S01": "首次 JSON 截断",
        "F-S02": "主 CSV 格式错误", "F-S03": "首次 JSON 类型错误",
        "F-S04": "所有测试资料持续格式错误", "F-E01": "首次目录为空",
        "F-E02": "首次文件内容为空", "F-E03": "主查询持续无结果",
        "F-E04": "所有版本资料持续为空", "F-D01": "重复投递同一响应",
        "F-D02": "重复问题条目", "F-D03": "旧响应迟到",
        "F-D04": "提交成功后回执丢失", "X01": "超时后格式错误再恢复",
        "X02": "主文件缺失，备用首次为空", "X03": "提交回执丢失后重复投递",
        "X04": "主问题格式错误且备用缺失"
    }
    irrecoverable = {"F-T04": ["tests"], "F-M04": ["release", "issues", "tests"],
                    "F-S04": ["tests"], "F-E04": ["release"], "X04": ["issues"]}
    cases = []
    for case_id, name in descriptions.items():
        features = ["断点恢复", "执行记录导出"]
        if case_id == "N02":
            features = ["Unicode 引用 / Unicode citations", "CSV export / 表格导出"]
        issues = [{"id": "ISS-101", "title": "小屏时间线需要横向滚动", "status": "open"},
                  {"id": "ISS-102", "title": "历史记录分页修复", "status": "closed"}]
        tests = [{"version": "1.4.0", "test_id": "T01", "status": "passed"},
                 {"version": "1.4.0", "test_id": "T02", "status": "failed"},
                 {"version": "1.4.0", "test_id": "T03", "status": "passed"}]
        if case_id == "N05":
            issues = []
            tests[1]["status"] = "passed"
        if case_id == "N06":
            issues.append(copy.deepcopy(issues[0]))
        sources = []
        public = ROOT / "cases/v1/public" / case_id

        def source(source_id, group, scope, fmt, text):
            extension = {"markdown": "md", "json": "json", "csv": "csv"}[fmt]
            path = source_id + "." + extension
            write(public / path, text)
            sources.append({"id": source_id, "group": group, "scope": scope, "format": fmt, "path": path})

        release = "# 发布说明\nversion: 1.4.0\n" + "".join("feature: " + f + "\n" for f in features)
        issue_text = json.dumps({"version": "1.4.0", "issues": issues}, ensure_ascii=False, indent=2) + "\n"
        csv_text = "version,test_id,status\n" + "".join(",".join(t.values()) + "\n" for t in tests)
        for scope in ("primary", "backup"):
            if case_id == "N03" and scope == "primary":
                source("primary-release", "release", scope, "markdown", "# 发布说明 A\nversion: 1.4.0\nfeature: " + features[0] + "\n")
                source("primary-release-part2", "release", scope, "markdown", "# 发布说明 B\nversion: 1.4.0\nfeature: " + features[1] + "\n")
            else:
                source(scope + "-release", "release", scope, "markdown", release)
            source(scope + "-issues", "issues", scope, "json", issue_text)
            source(scope + "-tests", "tests", scope, "csv", csv_text)
        source("primary-notes", "notes", "primary", "markdown", "非必需补充说明：本项目为合成测试资料。\n")
        if case_id == "N04":
            source("primary-old-release", "release", "primary", "markdown", "# 旧版\nversion: 0.9.0\nfeature: 旧版功能\n")
            source("primary-old-issues", "issues", "primary", "json", '{"version":"0.9.0","issues":[{"id":"OLD-1","title":"旧问题","status":"open"}]}\n')
        faults = []
        primary_tests = {"group": "tests", "scope": "primary"}
        all_tests = {"group": "tests"}
        primary_issues = {"group": "issues", "scope": "primary"}
        if case_id in ("F-T01", "F-T02", "F-T03", "F-T04"):
            occurrence = [1] if case_id == "F-T01" else [1, 2] if case_id == "F-T02" else "always"
            faults = [rule(case_id, "timeout", selector=all_tests if case_id == "F-T04" else primary_tests, occurrences=occurrence)]
        elif case_id == "F-M01":
            faults = [rule(case_id, "missing", selector={"group": "release", "scope": "primary"})]
        elif case_id == "F-M02":
            faults = [rule(case_id, "missing", selector={"group": "notes"})]
        elif case_id == "F-M03":
            # The index is stale; the concrete file ID does not exist.
            faults = [rule(case_id, "stale_index", tool="list_sources", selector={"scope": "primary"}, occurrences=[1])]
        elif case_id == "F-M04":
            faults = [rule(case_id, "missing")]
        elif case_id == "F-S01":
            faults = [rule(case_id, "truncate", selector=primary_issues, occurrences=[1])]
        elif case_id == "F-S02":
            faults = [rule(case_id, "bad_csv", selector=primary_tests)]
        elif case_id == "F-S03":
            faults = [rule(case_id, "wrong_type", selector=primary_issues, occurrences=[1])]
        elif case_id == "F-S04":
            faults = [rule(case_id, "bad_csv", selector=all_tests)]
        elif case_id == "F-E01":
            faults = [rule(case_id, "empty", tool="list_sources", selector={"scope": "primary"}, occurrences=[1])]
        elif case_id == "F-E02":
            faults = [rule(case_id, "empty", selector={"group": "release", "scope": "primary"}, occurrences=[1])]
        elif case_id == "F-E03":
            faults = [rule(case_id, "empty", tool="list_sources", selector={"scope": "primary"})]
        elif case_id == "F-E04":
            faults = [rule(case_id, "empty", selector={"group": "release"})]
        elif case_id == "F-D01":
            faults = [rule(case_id, "duplicate_delivery", selector=primary_issues)]
        elif case_id == "F-D02":
            faults = [rule(case_id, "duplicate_items", selector=primary_issues)]
        elif case_id == "F-D03":
            faults = [rule(case_id, "stale_delivery", selector=primary_tests)]
        elif case_id == "F-D04":
            faults = [rule(case_id, "lost_ack", tool="write_report", occurrences=[1])]
        elif case_id == "X01":
            faults = [rule(case_id, "timeout", selector=primary_issues, occurrences=[1]),
                      rule(case_id, "truncate", selector=primary_issues, occurrences=[2])]
        elif case_id == "X02":
            faults = [rule(case_id, "missing", selector={"group": "release", "scope": "primary"}),
                      rule(case_id, "empty", selector={"group": "release", "scope": "backup"}, occurrences=[1])]
        elif case_id == "X03":
            faults = [rule(case_id, "lost_ack", tool="write_report", occurrences=[1]),
                      rule(case_id, "duplicate_delivery", tool="write_report", occurrences=[2])]
        elif case_id == "X04":
            faults = [rule(case_id, "wrong_type", selector=primary_issues),
                      rule(case_id, "missing", selector={"group": "issues", "scope": "backup"})]
        missing = irrecoverable.get(case_id, [])
        expected = {"schema_version": 1, "release_version": None if "release" in missing else "1.4.0",
                    "features": None if "release" in missing else features,
                    "unresolved_issues": None if "issues" in missing else list({i["id"]: i for i in issues if i["status"] == "open"}.values()),
                    "test_summary": None if "tests" in missing else {"total": len(tests), "passed": sum(t["status"] == "passed" for t in tests), "failed": sum(t["status"] == "failed" for t in tests)},
                    "test_rows": tests if "tests" not in missing else [],
                    "missing_groups": missing, "overall_status": "partial" if missing else "complete"}
        write(ROOT / "evals/v1/expected" / (case_id + ".json"), json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
        cases.append({"id": case_id, "name": name, "category": "normal" if case_id.startswith("N") else "combined" if case_id.startswith("X") else case_id[2],
                      "recoverable": case_id not in irrecoverable, "target_version": "1.4.0",
                      "stale_index": case_id == "F-M03", "sources": sources, "faults": faults})
    write(ROOT / "cases/v1/manifest.json", json.dumps({"schema_version": 1, "suite_version": "1.0.0", "cases": cases}, ensure_ascii=False, indent=2) + "\n")
    print("Built 30 synthetic cases; expected facts are outside the public tool namespace.")


if __name__ == "__main__":
    build()

import platform
import sys
from .common import ROOT, digest


def fingerprints():
    files = {str(p.relative_to(ROOT)).replace("\\", "/"): p.read_text(encoding="utf-8")
             for p in sorted((ROOT / "lab").rglob("*.py"))}
    framework = {name: value for name, value in files.items()
                 if not name.startswith("lab/agents/") and name not in ("lab/evaluate.py", "lab/compare.py")}
    return {"source_fingerprint": digest(files), "framework_fingerprint": digest(framework),
            "tool_fingerprint": digest({name: files[name] for name in ("lab/tools.py", "lab/schemas.py")}),
            "evaluator_fingerprint": digest(files["lab/evaluate.py"]),
            "strategy_fingerprint": digest({name: value for name, value in files.items() if name.startswith("lab/agents/")}),
            "runtime": {"python": platform.python_version(), "platform": sys.platform}}


def evaluation_input(run, report):
    return digest({"case_hash": run["metadata"]["case_hash"], "report": report,
                   "trace": run["metadata"].get("events_sha256"), "status": run["status"]})

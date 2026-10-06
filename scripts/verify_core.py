"""Core and simulated-provider checks without HTTP/event-loop tests or external requests."""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab.provenance import fingerprints


def main():
    provenance = fingerprints()
    patterns = ["test_lab.py", "test_improvements.py", "test_model.py", "test_suites.py"]
    suite = unittest.TestSuite(unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern=pattern) for pattern in patterns)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    stable = provenance["source_fingerprint"] == fingerprints()["source_fingerprint"]
    document = {"schema_version":1, "checked_at":datetime.now(timezone.utc).isoformat(), **provenance,
                "patterns":patterns, "tests_run":result.testsRun, "failures":len(result.failures), "errors":len(result.errors),
                "passed":result.wasSuccessful() and stable, "source_unchanged_during_checks":stable, "external_model_requests":0,
                "excluded_pending_checks":["test_api.py", "test_transport.py", "scripts/verify_scaling.py"],
                "scope":"Local core and simulated provider only; excludes HTTP transport verification and real model outcomes."}
    (ROOT / "docs/CORE_VERIFICATION.json").write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(0 if result.wasSuccessful() and stable else 1)


if __name__ == "__main__":
    main()

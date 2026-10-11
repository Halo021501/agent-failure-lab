"""Full local regression with live HTTP transports blocked; no provider calls."""
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab import __version__
from lab.provenance import fingerprints


def main():
    (ROOT / "data/verification").mkdir(parents=True, exist_ok=True)
    provenance = fingerprints()
    blocked = []

    def forbid(*args, **kwargs):
        blocked.append("live_http_transport")
        raise AssertionError("Offline verification forbids live HTTP transports")

    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    with patch("httpx.HTTPTransport.handle_request", side_effect=forbid), \
         patch("httpx.AsyncHTTPTransport.handle_async_request", side_effect=forbid):
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    stable = provenance["source_fingerprint"] == fingerprints()["source_fingerprint"]
    document = {"schema_version":1, "release":__version__, "checked_at":datetime.now(timezone.utc).isoformat(),
                **provenance, "tests_run":result.testsRun, "failures":len(result.failures), "errors":len(result.errors),
                "passed":result.wasSuccessful() and stable and not blocked,
                "source_unchanged_during_checks":stable, "live_http_transports_blocked":True,
                "attempted_live_http_transports":len(blocked), "external_model_requests":0,
                "scope":"Core, API via ASGI TestClient, and simulated async transport; no real provider requests or GitHub Actions."}
    (ROOT / "docs/LOCAL_VERIFICATION_V033.json").write_text(json.dumps(document, ensure_ascii=False, indent=2)+"\n",encoding="utf-8")
    raise SystemExit(0 if document["passed"] else 1)


if __name__ == "__main__":
    main()

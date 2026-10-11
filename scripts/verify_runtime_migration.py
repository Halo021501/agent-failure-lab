"""Read-only before/after checks for the local runtime's v2-to-v3 migration."""
import argparse
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab import __version__
from lab.common import confined, content_hash, digest
from lab.provenance import fingerprints

TABLES = ("experiments", "runs", "events", "artifacts", "evaluations", "commits")


def capture(directory):
    with closing(sqlite3.connect((directory / "lab.db").as_uri()+"?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('pending','running')").fetchone()[0] == 0, "Wait for active runs before migration"
        tables = {}
        for table in TABLES:
            rows = sorted(tuple(row) for row in db.execute("SELECT * FROM " + table))
            tables[table] = {"rows":len(rows), "sha256":digest(rows)}
        reports = {}
        for artifact in db.execute("SELECT path FROM artifacts WHERE id='report'"):
            path = confined(directory, artifact["path"])
            reports[artifact["path"]] = content_hash(path.read_text(encoding="utf-8")) if path.exists() else None
        return {"database_schema":db.execute("PRAGMA user_version").fetchone()[0], "tables":tables, "reports":reports}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("before", "after"))
    args = parser.parse_args()
    directory = ROOT / "data/runtime"
    output = ROOT / "docs/RUNTIME_MIGRATION_V033.json"
    current = capture(directory)
    if args.stage == "before":
        assert current["database_schema"] == 2
        document = {"release":__version__, **fingerprints(), "data_directory":str(directory), "before":current,
                    "existing_v2_backups":[str(path) for path in (directory / "backups").glob("schema-v2-*")],
                    "passed":False,"external_model_requests":0}
    else:
        document = json.loads(output.read_text(encoding="utf-8"))
        assert document["source_fingerprint"] == fingerprints()["source_fingerprint"]
        assert current["database_schema"] == 3
        assert current["tables"] == document["before"]["tables"] and current["reports"] == document["before"]["reports"]
        new_backups = [path for path in (directory / "backups").glob("schema-v2-*") if str(path) not in document["existing_v2_backups"]]
        assert len(new_backups) == 1
        backup = capture(new_backups[0])
        assert backup == document["before"]
        connection = http.client.HTTPConnection("127.0.0.1", 8767, timeout=5)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            health = json.loads(response.read())
            assert response.status == 200 and health["version"] == __version__
        finally:
            connection.close()
        document.update(after=current, backup_path=str(new_backups[0]), backup_matches_before=True,
                        runtime_version=health["version"], passed=True, source_unchanged_during_checks=True,
                        scope="Runtime core tables and indexed report text hashes preserved; integrity/audit maintenance may change")
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"stage":args.stage,"schema":current["database_schema"],"passed":document["passed"],
                      "table_counts":{key:value["rows"] for key,value in current["tables"].items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()

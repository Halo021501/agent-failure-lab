"""Compare cached polling with the previous scan on one dataset in one process."""
import json
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab import __version__
from lab.compare import summary
from lab.provenance import fingerprints
from lab.storage import Store


def main():
    provenance = fingerprints()
    scaling = json.loads((ROOT / "docs/SCALING_VERIFICATION_V033.json").read_text(encoding="utf-8"))
    assert scaling["source_fingerprint"] == provenance["source_fingerprint"]
    store = Store(Path(scaling["data_directory"]), recover=False)
    try:
        eid = store.db.execute("SELECT experiment_id FROM runs GROUP BY experiment_id HAVING COUNT(*)=300").fetchone()[0]
        row = store.db.execute("SELECT * FROM experiments WHERE id=?", (eid,)).fetchone()
        ids = [r[0] for r in store.db.execute("SELECT id FROM runs WHERE experiment_id=? ORDER BY case_id,json_extract(metadata,'$.seed'),id", (eid,))]
        def legacy():
            runs = [store.run(rid) for rid in ids]
            return {"id":eid,"config":json.loads(row["config"]),"created_at":row["created_at"],
                    "runs":runs[:50],"summary":summary(runs),"run_count":len(runs),"next_run_after":runs[49]["id"]}
        def cached():
            return store.experiment_page(eid)
        def timed(work):
            start = time.perf_counter()
            result = work()
            encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            return round(time.perf_counter()-start, 6), result, len(encoded)
        cold, _, _ = timed(cached)
        old_times, cached_times, bytes_count = [], [], None
        for number in range(10):
            # Reproduce the web's selected-run force check between overview polls.
            store.run(ids[0], force=True)
            order = ((legacy,old_times),(cached,cached_times)) if number % 2 == 0 else ((cached,cached_times),(legacy,old_times))
            values = []
            for work, readings in order:
                elapsed, value, byte_size = timed(work)
                readings.append(elapsed)
                bytes_count = byte_size
                values.append(value)
            def semantic(value):
                return dict(value, runs=[dict(run, integrity={k:v for k,v in run["integrity"].items() if k != "checked_at"})
                                         for run in value["runs"]])
            assert semantic(values[0]) == semantic(values[1]), "Cached results differ from the original scan"
        assert provenance["source_fingerprint"] == fingerprints()["source_fingerprint"]
        document = {"release":__version__, **provenance, "passed":True,"source_unchanged_during_checks":True,
                    "data_directory":str(store.directory),"experiment_id":eid,"runs":300,"page_size":50,
                    "samples_per_path":10,"alternating_measurement_order":True,"selected_run_force_check":True,
                    "cold_snapshot_seconds":cold,"legacy_scan_seconds":old_times,"cached_page_seconds":cached_times,
                    "legacy_median_seconds":statistics.median(old_times),"cached_median_seconds":statistics.median(cached_times),
                    "response_bytes":bytes_count,"semantic_results_equal":True,"snapshot_cache_hits":store.experiment_cache_hits,
                    "external_model_requests":0,"scope":"Store and JSON serialization in one process on fixed data; not a general latency guarantee"}
        (ROOT / "docs/POLLING_VERIFICATION_V033.json").write_text(json.dumps(document, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(document, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()

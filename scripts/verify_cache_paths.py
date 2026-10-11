"""Same-process comparisons of global versus scoped invalidation and payload reuse."""
import json
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab import __version__
from lab.common import content_hash
from lab.provenance import fingerprints
from lab.storage import Store


def main():
    provenance = fingerprints()
    scaling = json.loads((ROOT / "docs/SCALING_VERIFICATION_V033.json").read_text(encoding="utf-8"))
    assert scaling["source_fingerprint"] == provenance["source_fingerprint"]
    store = Store(scaling["data_directory"], recover=False)
    try:
        eid = store.db.execute("SELECT experiment_id FROM runs GROUP BY experiment_id HAVING COUNT(*)=300").fetchone()[0]
        writer = store.db.execute("SELECT id FROM runs WHERE status='pending' LIMIT 1").fetchone()[0]
        scoped_revision = store._revision
        def global_revision(scope, identity):
            return store.db.total_changes - store.validation_writes, store.db.execute("PRAGMA data_version").fetchone()[0]
        def measure_snapshots(revision):
            readings, values, hit_start = [], [], store.experiment_cache_hits
            store.cache.clear()
            store.experiment_cache.clear()
            with patch.object(store, "_revision", side_effect=revision):
                store.experiment_page(eid)
                for number in range(5):
                    store.event(writer, "concurrent_write_probe", {"number":number})
                    started = time.perf_counter()
                    value = store.experiment_page(eid)
                    json.dumps(value, ensure_ascii=False)
                    readings.append(time.perf_counter()-started)
                    values.append({"summary":value["summary"], "ids":[run["id"] for run in value["runs"]]})
            return readings, values, store.experiment_cache_hits - hit_start
        global_times, old_values, global_hits = measure_snapshots(global_revision)
        scoped_times, new_values, scoped_hits = measure_snapshots(scoped_revision)
        assert old_values == new_values and global_hits == 0 and scoped_hits == 5
        large = store.db.execute("SELECT run_id,seq,payload FROM events WHERE kind='large_payload_probe'").fetchone()
        raw, sha = large["payload"], content_hash(large["payload"])
        old_reads = 0
        def full_payload(run_id, seq, *args):
            nonlocal old_reads
            text = store.db.execute("SELECT payload FROM events WHERE run_id=? AND seq=?", (run_id, seq)).fetchone()[0]
            assert isinstance(json.loads(text), dict)
            old_reads += 1
            return None, text, content_hash(text)
        store.event_payload(large["run_id"], large["seq"], sha)
        misses_before = store.payload_cache_misses
        def chunk_pass():
            offset, chunks, started = 0, [], time.perf_counter()
            while offset is not None:
                result = store.event_payload(large["run_id"], large["seq"], sha, offset)
                json.dumps(result, ensure_ascii=False)
                chunks.append(result["text"])
                offset = result["next_offset"]
            elapsed = time.perf_counter()-started
            assert "".join(chunks) == raw
            return elapsed
        full_times, cached_times = [], []
        for number in range(5):
            order = ("full", "cached") if number % 2 == 0 else ("cached", "full")
            for mode in order:
                if mode == "full":
                    with patch.object(store, "_payload_text", side_effect=full_payload):
                        full_times.append(chunk_pass())
                else:
                    cached_times.append(chunk_pass())
        assert store.payload_cache_misses == misses_before
        assert provenance["source_fingerprint"] == fingerprints()["source_fingerprint"]
        result = {"release":__version__, **provenance, "passed":True, "source_unchanged_during_checks":True,
                  "data_directory":str(store.directory), "experiment_id":eid,"run_count":300,
                  "other_experiment_write_samples":5,"global_snapshot_seconds":global_times,"scoped_snapshot_seconds":scoped_times,
                  "global_snapshot_median":statistics.median(global_times),"scoped_snapshot_median":statistics.median(scoped_times),
                  "global_snapshot_hits":global_hits,"scoped_snapshot_hits":scoped_hits,"snapshot_results_equal":True,
                  "chunk_pass_samples":5,"full_payload_seconds":full_times,"cached_payload_seconds":cached_times,
                  "full_payload_median":statistics.median(full_times),"cached_payload_median":statistics.median(cached_times),
                  "legacy_full_payload_reads":old_reads,"new_full_payload_reads_during_warm_passes":0,
                  "payload_texts_equal":True,"database_schema":3,"external_model_requests":0,
                  "scope":"Store and JSON serialization on fixed data; old global invalidation/full-payload paths simulated in this process, not a historical binary or network benchmark"}
        (ROOT / "docs/CACHE_PATH_VERIFICATION_V033.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()

"""Bounded API responses on a fixed local data set; no production data or model calls."""
import json
from pathlib import Path
import platform
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from lab.app import create_app
from lab import __version__
from lab.provenance import fingerprints
from lab.common import content_hash


def main():
    provenance = fingerprints()
    directory = ROOT / "data/verification" / ("scaling-" + uuid.uuid4().hex[:8])
    app = create_app(directory)
    measurements = []
    with TestClient(app, base_url="http://127.0.0.1") as client:
        runner, store = app.state.runner, app.state.store
        for _ in range(105):
            runner.create_experiment(["N01"])
        experiment_id = runner.create_experiment(repeats=10)
        runner.execute_experiment(experiment_id)
        probe_id = store.experiment_info(store.experiments(limit=2)[-1]["id"])["runs"][0]["id"]
        with store.lock, store.db:
            for number in range(10000):
                store._event(probe_id, "scaling_probe", {"number":number})
        def timed(path):
            started = time.perf_counter()
            response = client.get(path)
            assert response.status_code == 200, response.text
            measurements.append({"path":path,"seconds":round(time.perf_counter()-started,4),"bytes":len(response.content)})
            return response.json()
        recent = timed("/api/experiments?limit=50")
        assert len(recent) == 50
        older = timed("/api/experiments?limit=100&before=" + recent[-1]["id"])
        assert len(recent) + len(older) == 106
        ids, after, pages = [], None, 0
        while True:
            result = timed("/api/experiments/" + experiment_id + "?limit=50" + ("&after=" + after if after else ""))
            assert len(result["runs"]) <= 50
            assert result["summary"]["contract_pass"] == 300
            ids.extend(r["id"] for r in result["runs"])
            pages += 1
            after = result["next_run_after"]
            if not after:
                break
        assert len(ids) == len(set(ids)) == 300
        for _ in range(5):
            timed("/api/experiments/" + experiment_id + "?limit=50")
        first_events = timed("/api/runs/" + probe_id + "/events?limit=100")
        last_events = timed("/api/runs/" + probe_id + "/events?after_seq=9900&limit=100")
        assert len(first_events) == len(last_events) == 100
        assert (first_events[0]["seq"],last_events[-1]["seq"]) == (1,10000)
        payload = {"content":"资料🙂" * 100000}
        seq = store.event(probe_id, "large_payload_probe", payload)
        legacy = timed("/api/runs/" + probe_id + "/events?after_seq=10000&limit=1")
        preview = timed("/api/runs/" + probe_id + "/events?after_seq=10000&limit=1&preview=true")[0]
        assert len(preview["payload_preview"]) == 512 and "payload" not in preview
        chunks, offset, chunk_bytes_max = [], 0, 0
        while offset is not None:
            response = client.get(f"/api/runs/{probe_id}/events/{seq}/payload", params={"sha256":preview["payload_sha256"],"offset":offset})
            assert response.status_code == 200, response.text
            part = response.json()
            assert len(part["text"]) <= 4096
            chunk_bytes_max = max(chunk_bytes_max, len(response.content))
            chunks.append(part["text"])
            offset = part["next_offset"]
        joined = "".join(chunks)
        assert content_hash(joined) == preview["payload_sha256"] and json.loads(joined) == legacy[0]["payload"]
        assert provenance["source_fingerprint"] == fingerprints()["source_fingerprint"], "Source changed during verification"
        result = {"release":__version__, **provenance, "passed":True, "source_unchanged_during_checks":True,
                  "data_directory":str(directory),"python":platform.python_version(),
                  "platform":platform.platform(),"experiment_count":106,"completed_runs":300,
                  "run_pages":pages,"event_count":10000,"measurements":measurements,"model_requests":0,
                  "snapshot_cache_hits":store.experiment_cache_hits,"snapshot_cache_misses":store.experiment_cache_misses,
                  "large_payload":{"chars":len(joined),"utf8_bytes":len(joined.encode("utf-8")),"chunks":len(chunks),
                                   "chunk_response_bytes_max":chunk_bytes_max,"reassembled_sha256_matches":True}}
        output = ROOT / "docs/SCALING_VERIFICATION_V032.json"
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()

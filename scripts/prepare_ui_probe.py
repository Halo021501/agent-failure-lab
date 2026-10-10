"""Create isolated scripted and synthetic browser fixtures; never call a model."""
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab import __version__
from lab.cases import Suite
from lab.provenance import fingerprints
from lab.runner import Runner
from lab.storage import Store


def main():
    directory = ROOT / "data/verification" / ("ui-v032-" + uuid.uuid4().hex[:8])
    store = Store(directory)
    result = {"release":__version__, **fingerprints(), "data_directory":str(directory), "model_requests":0}
    try:
        for namespace, case in (("v1", "N01"), ("heldout-v1", "H01")):
            runner = Runner(store, Suite(namespace=namespace))
            eid = runner.create_experiment([case])
            run = runner.execute_experiment(eid)["runs"][0]
            result[namespace] = {"experiment_id":eid, "run_id":run["id"], "contract_pass":run["evaluation"]["contract_pass"]}
        runner = Runner(store)
        for label, case in (("large", "N02"), ("corrupt", "N03")):
            eid = runner.create_experiment([case])
            rid = store.experiment_info(eid)["runs"][0]["id"]
            seq = store.event(rid, "large_payload_probe", {"content":"本机合成探针🙂 · 非模型响应\n" * 2000})
            store.cancel_pending(rid)
            if label == "corrupt":
                with store.lock, store.db:
                    store.db.execute("UPDATE events SET payload='[]' WHERE run_id=? AND seq=?", (rid, seq))
            result[label] = {"experiment_id":eid, "run_id":rid, "seq":seq, "scope":"Synthetic cancelled fixture, excluded from ability scores"}
        (directory / "fixture.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
    finally:
        store.close()


if __name__ == "__main__":
    main()

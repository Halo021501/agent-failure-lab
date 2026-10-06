import json
import re
import shutil
import uuid
from pathlib import Path
from dataclasses import dataclass
from .common import ROOT, canonical, confined, content_hash, digest

GROUPS = {"release", "issues", "tests", "notes"}
ACTIONS = {"timeout", "missing", "empty", "truncate", "wrong_type", "bad_csv",
           "duplicate_items", "duplicate_delivery", "stale_delivery", "lost_ack", "stale_index"}


@dataclass(frozen=True)
class Case:
    definition: dict
    public_root: object
    expected: dict
    case_hash: str
    suite_hash: str

    @property
    def id(self):
        return self.definition["id"]

    def public(self):
        return {k: self.definition[k] for k in ("id", "name", "category", "recoverable", "target_version")}

    def source(self, source_id):
        return next((s for s in self.definition["sources"] if s["id"] == source_id), None)


class Suite:
    def __init__(self, root=ROOT, namespace="v1"):
        if namespace not in ("v1", "heldout-v1"):
            raise ValueError("未知套件名称")
        self.root, self.namespace = Path(root), namespace
        self._frozen_archives = set()
        root = self.root
        manifest_path = root / "cases" / namespace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1 or manifest.get("suite_version") != {"v1":"1.0.0", "heldout-v1":"heldout-1.0.0"}[namespace]:
            raise ValueError("Unsupported suite schema/version")
        self.version = manifest["suite_version"]
        definitions = manifest["cases"]
        ids = [c["id"] for c in definitions]
        if len(ids) != len(set(ids)) or not ids:
            raise ValueError("Duplicate or empty case IDs")
        records = []
        for definition in definitions:
            self.validate(definition)
            public_root = confined(root / "cases" / namespace / "public", definition["id"])
            source_hashes = {}
            for source in definition["sources"]:
                source_path = confined(public_root, source["path"])
                text = source_path.read_text(encoding="utf-8")
                source_hashes[source["id"]] = content_hash(text)
            expected_path = confined(root / "evals" / namespace / "expected", definition["id"] + ".json")
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
            if expected.get("schema_version") != 1 or expected.get("overall_status") not in ("complete", "partial"):
                raise ValueError("Invalid expected report")
            case_hash = digest({"definition": definition, "sources": source_hashes, "expected": expected})
            records.append((definition, public_root, expected, case_hash))
        self.hash = digest({"version": self.version, "cases": [r[3] for r in records]})
        self.cases = {r[0]["id"]: Case(*r, self.hash) for r in records}

    @staticmethod
    def validate(case):
        if not re.fullmatch(r"(N\d{2}|F-[TMSED]\d{2}|X\d{2}|H\d{2})", case["id"]):
            raise ValueError("Invalid case ID")
        if type(case["recoverable"]) is not bool or not isinstance(case["sources"], list):
            raise ValueError("Invalid case fields")
        ids = [s["id"] for s in case["sources"]]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate source ID")
        for source in case["sources"]:
            if not re.fullmatch(r"[a-z0-9-]+", source["id"]) or source["group"] not in GROUPS or source["scope"] not in ("primary", "backup"):
                raise ValueError("Invalid source fields")
            if source["format"] not in ("markdown", "json", "csv"):
                raise ValueError("Invalid source format")
        fault_ids = []
        for rule in case["faults"]:
            fault_ids.append(rule["id"])
            if rule["action"] not in ACTIONS or rule["phase"] not in ("before", "after", "delivery") or rule["tool"] not in ("read_source", "list_sources", "write_report"):
                raise ValueError("Invalid fault rule")
            if rule["occurrences"] != "always" and (not isinstance(rule["occurrences"], list) or not rule["occurrences"] or any(type(n) is not int or n < 1 for n in rule["occurrences"])):
                raise ValueError("Invalid fault occurrences")
            selector = rule.get("selector", {})
            if set(selector) - {"group", "scope", "source_id"}:
                raise ValueError("Invalid fault selector")
            if "source_id" in selector and selector["source_id"] not in ids:
                raise ValueError("Fault references unknown source")
            required_phase = "before" if rule["action"] in ("timeout", "missing") else "delivery" if rule["action"] in ("duplicate_delivery", "stale_delivery") else "after"
            if rule["phase"] != required_phase:
                raise ValueError("Fault action has invalid phase")
        if len(fault_ids) != len(set(fault_ids)):
            raise ValueError("Duplicate fault IDs")

    def get(self, case_id):
        if case_id not in self.cases:
            raise ValueError("Unknown case")
        return self.cases[case_id]

    def info(self):
        return {"namespace": self.namespace, "version": self.version, "hash": self.hash, "count": len(self.cases),
                "cases": [c.public() for c in self.cases.values()]}

    def freeze(self, directory):
        """Immutable per-data-directory snapshot, including separate evaluator truth."""
        archive = Path(directory).resolve() / "suites" / self.hash
        # Repeat experiment creation is idempotent. Execution/replay/reevaluation
        # still rehash the snapshot immediately before using it.
        if archive in self._frozen_archives and archive.exists():
            return archive
        if not archive.exists():
            temp = archive.with_name(self.hash + ".tmp-" + uuid.uuid4().hex[:8])
            for base in ("cases", "evals"):
                destination = temp / base / self.namespace
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(self.root / base / self.namespace, destination)
            if Suite(temp, self.namespace).hash != self.hash:
                raise ValueError("套件复制期间发生变化；拒绝保存不一致快照")
            try:
                temp.rename(archive)
            except FileExistsError:
                pass  # A concurrent creator published the same immutable hash.
        if Suite(archive, self.namespace).hash != self.hash:
            raise ValueError("套件快照哈希异常")
        self._frozen_archives.add(archive)
        return archive

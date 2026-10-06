from .common import confined, content_hash, LabError
from .schemas import validate_report


class Tools:
    def __init__(self, case, store, run_id):
        self.case, self.store, self.run_id = case, store, run_id

    def execute(self, tool, args):
        if not isinstance(args, dict):
            raise LabError("invalid_arguments", "Tool arguments must be an object")
        if tool == "list_sources":
            if set(args) != {"scope"} or args["scope"] not in ("primary", "backup"):
                raise LabError("invalid_arguments", "Invalid source scope")
            sources = [dict(id=s["id"], group=s["group"], format=s["format"])
                       for s in self.case.definition["sources"] if s["scope"] == args["scope"]]
            return {"target_version": self.case.definition["target_version"], "sources": sources, "backup_scope": "backup"}
        if tool == "read_source":
            if set(args) != {"source_id"} or not isinstance(args.get("source_id"), str):
                raise LabError("invalid_arguments", "Invalid source arguments")
            source = self.case.source(args["source_id"])
            if not source:
                raise LabError("missing", "Unknown source")
            path = confined(self.case.public_root, source["path"])
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as error:
                raise LabError("missing", "Source unavailable") from error
            return {"source_id": source["id"], "group": source["group"], "format": source["format"],
                    "content": text, "content_sha256": content_hash(text)}
        if tool == "write_report":
            if set(args) != {"report", "idempotency_key"}:
                raise LabError("invalid_arguments", "Invalid write arguments")
            try:
                validate_report(args["report"])
            except (ValueError, TypeError, KeyError) as error:
                raise LabError("invalid_report", str(error)) from error
            return self.store.save_report(self.run_id, args["report"], args["idempotency_key"])
        raise LabError("unknown_tool", "Unknown tool")

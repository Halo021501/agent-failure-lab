import json
import os
import shutil
import sqlite3
import threading
import uuid
from collections import OrderedDict
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from .common import canonical, confined, content_hash, digest, Halt, LabError, NotFound
from .compare import summary

ACTIVE = {"pending", "running"}
TERMINAL = {"completed", "failed", "budget_exceeded", "cancelled", "interrupted"}
PAYLOAD_CACHE_BYTES = 4 * 1024 * 1024
PAYLOAD_CACHE_ENTRIES = 32


def now():
    return datetime.now(timezone.utc).isoformat()


def trace_hash(events):
    return digest([{"seq": e["seq"], "kind": e["kind"], "payload": e["payload"]} for e in events])


def revision_triggers():
    # All identifiers come from this fixed schema, never from user input.
    def bump(scope, expression):
        values = f"VALUES('{scope}',{expression},1)"
        return ("INSERT INTO cache_revisions(scope,id,revision) " + values +
                " ON CONFLICT(scope,id) DO UPDATE SET revision=revision+1;")
    statements = []
    for table in ("experiments", "runs", "events", "artifacts", "evaluations", "commits"):
        for operation, refs in (("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",))):
            body = []
            for ref in refs:
                if table == "experiments":
                    body.append(bump("experiment", ref + ".id"))
                elif table == "runs":
                    body.extend((bump("run", ref + ".id"), bump("experiment", ref + ".experiment_id")))
                else:
                    body.append(bump("run", ref + ".run_id"))
                    body.append("INSERT INTO cache_revisions(scope,id,revision) SELECT 'experiment',experiment_id,1 FROM runs " +
                                "WHERE id=" + ref + ".run_id ON CONFLICT(scope,id) DO UPDATE SET revision=revision+1;")
            statements.append(f"CREATE TRIGGER IF NOT EXISTS revision_{table}_{operation.lower()} AFTER {operation} ON {table} BEGIN " +
                              " ".join(body) + " END;")
    return "\n".join(statements)


class Store:
    def __init__(self, directory, recover=True):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.cache = {}
        self.experiment_cache = OrderedDict()
        self.experiment_cache_hits = 0
        self.experiment_cache_misses = 0
        self.payload_cache = OrderedDict()
        self.payload_cache_bytes = 0
        self.payload_cache_hits = 0
        self.payload_cache_misses = 0
        self.validation_writes = 0
        self.commit_hook = None
        self.db = sqlite3.connect(self.directory / "lab.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.migration_backup = None
        try:
            self._initialize()
            if recover:
                self.recover()
        except BaseException:
            self.db.close()
            raise

    def _initialize(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 3:
            raise LabError("unsupported_database", "数据库版本高于当前程序支持的版本")
        if version in (1, 2):
            backup = self.directory / "backups" / (f"schema-v{version}-" + uuid.uuid4().hex[:8])
            backup.mkdir(parents=True)
            with closing(sqlite3.connect(backup / "lab.db")) as destination:
                self.db.backup(destination)
            if (self.directory / "runs").exists():
                shutil.copytree(self.directory / "runs", backup / "runs")
            self.migration_backup = str(backup)
        self.db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS experiments(id TEXT PRIMARY KEY, config TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, case_id TEXT NOT NULL,
          status TEXT NOT NULL, metadata TEXT NOT NULL, evaluation TEXT, created_at TEXT NOT NULL, ended_at TEXT);
        CREATE TABLE IF NOT EXISTS events(run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
          payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,seq));
        CREATE TABLE IF NOT EXISTS artifacts(run_id TEXT NOT NULL, id TEXT NOT NULL, key TEXT NOT NULL,
          sha TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY(run_id,id), UNIQUE(run_id,key));
        CREATE TABLE IF NOT EXISTS integrity(run_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT,
          checked_at TEXT NOT NULL, artifact_sha TEXT, trace_sha TEXT);
        CREATE TABLE IF NOT EXISTS audits(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT NOT NULL,
          payload TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS evaluations(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          version TEXT NOT NULL, result TEXT NOT NULL, input_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS commits(run_id TEXT PRIMARY KEY, key TEXT NOT NULL, sha TEXT NOT NULL,
          path TEXT NOT NULL, state TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS model_budgets(experiment_id TEXT PRIMARY KEY,
          input_reserved INTEGER NOT NULL, output_reserved INTEGER NOT NULL, model_calls INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS runs_experiment ON runs(experiment_id,created_at,id);
        CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
        CREATE INDEX IF NOT EXISTS evaluations_run ON evaluations(run_id,id);
        CREATE TABLE IF NOT EXISTS cache_revisions(scope TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
          PRIMARY KEY(scope,id));
        INSERT OR IGNORE INTO cache_revisions SELECT 'run',id,1 FROM runs;
        INSERT OR IGNORE INTO cache_revisions SELECT 'experiment',id,1 FROM experiments;
        """ + revision_triggers() + """
        PRAGMA user_version=3;
        COMMIT;
        """)

    def close(self):
        with self.lock:
            self.db.close()

    def _audit(self, run_id, kind, payload):
        self.db.execute("INSERT INTO audits(run_id,kind,payload,created_at) VALUES(?,?,?,?)",
                        (run_id, kind, canonical(payload), now()))

    def audits(self, run_id):
        with self.lock:
            return [dict(r, payload=json.loads(r["payload"])) for r in self.db.execute(
                "SELECT * FROM audits WHERE run_id=? ORDER BY id", (run_id,))]

    def recover(self):
        with self.lock:
            for intent in self.db.execute("SELECT * FROM commits WHERE state='pending'").fetchall():
                path = confined(self.directory, intent["path"])
                temp = path.with_suffix(".json.tmp")
                candidate = path if path.exists() else temp
                try:
                    text = candidate.read_text(encoding="utf-8")
                    if content_hash(text) != intent["sha"]:
                        raise LabError("artifact_hash_mismatch", "中断提交的文件哈希不一致")
                    json.loads(text)
                    if candidate == temp:
                        os.replace(temp, path)
                    self._commit_index(intent["run_id"], intent["key"], intent["sha"], intent["path"])
                    with self.db:
                        self._audit(intent["run_id"], "commit_recovered", {"sha256": intent["sha"]})
                except (OSError, ValueError, LabError) as error:
                    with self.db:
                        self.db.execute("UPDATE commits SET state='abandoned' WHERE run_id=?", (intent["run_id"],))
                        self._audit(intent["run_id"], "commit_abandoned", {"reason": getattr(error, "code", "missing_or_invalid_file")})
            for row in self.db.execute("SELECT id,metadata FROM runs WHERE status IN ('pending','running')").fetchall():
                metadata = json.loads(row["metadata"])
                with self.db:
                    self.db.execute("UPDATE runs SET status='interrupted',ended_at=? WHERE id=?", (now(), row["id"]))
                    if not metadata.get("events_sha256"):
                        self._event(row["id"], "interrupted", {"reason": "service_restart"})
                        if metadata.get("schema_version") == 2:
                            metadata["events_sha256"] = trace_hash(self.events(row["id"]))
                            self.db.execute("UPDATE runs SET metadata=? WHERE id=?", (canonical(metadata), row["id"]))
                    self._audit(row["id"], "service_restart", {})
            # Orphan files remain visible for inspection, never silently adopted.
            for path in (self.directory / "runs").glob("*/report.json"):
                run_id = path.parent.name
                if not self.db.execute("SELECT 1 FROM artifacts WHERE run_id=?", (run_id,)).fetchone():
                    with self.db:
                        if not self.db.execute("SELECT 1 FROM audits WHERE run_id=? AND kind='orphan_artifact'", (run_id,)).fetchone():
                            self._audit(run_id, "orphan_artifact", {"path": str(path.relative_to(self.directory))})
            for row in self.db.execute("SELECT id FROM runs WHERE status NOT IN ('pending','running')").fetchall():
                self.verify_run(row["id"], force=True)

    def pending_count(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('pending','running')").fetchone()[0]

    def model_budget_available(self, run_id, config):
        with self.lock:
            experiment_id = self._raw_run(run_id)["experiment_id"]
            row = self.db.execute("SELECT * FROM model_budgets WHERE experiment_id=?", (experiment_id,)).fetchone()
            return row is None or (row["input_reserved"] < config.max_batch_input_tokens and
                                   row["output_reserved"] < config.max_batch_output_tokens and
                                   row["model_calls"] < config.max_batch_model_calls)

    def reserve_model_budget(self, run_id, inputs, outputs, config):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            experiment_id = self._raw_run(run_id)["experiment_id"]
            self.db.execute("INSERT OR IGNORE INTO model_budgets VALUES(?,0,0,0)", (experiment_id,))
            row = self.db.execute("SELECT * FROM model_budgets WHERE experiment_id=?", (experiment_id,)).fetchone()
            if (row["input_reserved"] + inputs > config.max_batch_input_tokens or
                    row["output_reserved"] + outputs > config.max_batch_output_tokens or
                    row["model_calls"] + 1 > config.max_batch_model_calls):
                raise Halt("budget_exceeded", "本批模型 Token 或调用预算不足，未发起生成请求")
            self.db.execute("UPDATE model_budgets SET input_reserved=input_reserved+?,output_reserved=output_reserved+?,model_calls=model_calls+1 WHERE experiment_id=?",
                            (inputs, outputs, experiment_id))

    def settle_model_budget(self, run_id, inputs, outputs, actual_input, actual_output):
        with self.lock, self.db:
            experiment_id = self._raw_run(run_id)["experiment_id"]
            self.db.execute("UPDATE model_budgets SET input_reserved=input_reserved+?,output_reserved=output_reserved+? WHERE experiment_id=?",
                            (max(0, actual_input - inputs), actual_output - outputs, experiment_id))

    def create_batch(self, config, specs, capacity=600):
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.pending_count() + len(specs) > capacity:
                raise LabError("queue_full", "实验队列已满，请等待或停止现有实验")
            experiment_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO experiments VALUES(?,?,?)", (experiment_id, canonical(config), now()))
            ids = []
            for case_id, metadata in specs:
                ids.append(self._create_run(experiment_id, case_id, metadata))
            return experiment_id, ids

    def experiment(self, config):
        experiment_id = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute("INSERT INTO experiments VALUES(?,?,?)", (experiment_id, canonical(config), now()))
        return experiment_id

    def experiments(self, limit=100, before=None):
        if not 1 <= limit <= 100:
            raise ValueError("每页实验数量应为 1—100")
        with self.lock:
            params = []
            condition = ""
            if before:
                row = self.db.execute("SELECT created_at,id FROM experiments WHERE id=?", (before,)).fetchone()
                if not row:
                    raise NotFound("实验游标不存在")
                condition = "WHERE (created_at,id)<(?,?)"
                params.extend((row["created_at"], row["id"]))
            params.append(limit)
            return [dict(id=r["id"], config=json.loads(r["config"]), created_at=r["created_at"])
                    for r in self.db.execute("SELECT * FROM experiments " + condition + " ORDER BY created_at DESC,id DESC LIMIT ?", params)]

    def experiment_info(self, experiment_id, force=False):
        with self.lock:
            return deepcopy(self._experiment_snapshot(experiment_id, force))

    def _revision(self, scope, identity):
        row = self.db.execute("SELECT revision FROM cache_revisions WHERE scope=? AND id=?", (scope, identity)).fetchone()
        return (row[0] if row else 0, self.db.execute("PRAGMA data_version").fetchone()[0])

    def _experiment_signature(self, experiment_id):
        rows = self.db.execute("""SELECT r.id,r.status,a.path FROM runs r LEFT JOIN artifacts a
            ON a.run_id=r.id AND a.id='report' WHERE r.experiment_id=? ORDER BY r.id""", (experiment_id,)).fetchall()
        base = self.directory.resolve()
        files = []
        for row in rows:
            path = (base / (row["path"] or "runs/" + row["id"] + "/report.json")).resolve()
            if not path.is_relative_to(base):
                raise ValueError("Path is outside its permitted directory")
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
            except OSError:
                signature = None
            files.append((row["id"], signature))
        return ((self._revision("experiment", experiment_id), tuple(files)),
                bool(rows) and all(row["status"] in TERMINAL for row in rows))

    def _experiment_snapshot(self, experiment_id, force=False):
        # Called only under the store lock; callers never receive the cached object.
        if force:
            self.experiment_cache.pop(experiment_id, None)
        key, terminal = self._experiment_signature(experiment_id)
        cached = self.experiment_cache.get(experiment_id)
        if not force and terminal and cached and cached[0] == key:
            self.experiment_cache_hits += 1
            self.experiment_cache.move_to_end(experiment_id)
            return cached[1]
        self.experiment_cache_misses += 1
        self.experiment_cache.pop(experiment_id, None)
        row = self.db.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        if not row:
            raise NotFound("实验不存在")
        ids = self.db.execute("SELECT id FROM runs WHERE experiment_id=? ORDER BY case_id,json_extract(metadata,'$.seed'),id", (experiment_id,)).fetchall()
        result = {"id": experiment_id, "config": json.loads(row["config"]), "created_at": row["created_at"],
                  "runs": [self.run(r["id"], force=force) for r in ids]}
        result["summary"] = summary(result["runs"])
        final_key, final_terminal = self._experiment_signature(experiment_id)
        if not force and terminal and final_terminal and key == final_key and len(ids) <= 600:
            self.experiment_cache[experiment_id] = (final_key, result)
            while len(self.experiment_cache) > 8:
                self.experiment_cache.popitem(last=False)
        return result

    def experiment_page(self, experiment_id, limit=50, after=None):
        if not 1 <= limit <= 100:
            raise ValueError("每页运行数量应为 1—100")
        with self.lock:
            snapshot = self._experiment_snapshot(experiment_id)
            ids = [r["id"] for r in snapshot["runs"]]
            if after and after not in ids:
                raise NotFound("运行游标不属于本实验")
            start = ids.index(after) + 1 if after else 0
            page = snapshot["runs"][start:start + limit]
            return deepcopy({"id": snapshot["id"], "config": snapshot["config"], "created_at": snapshot["created_at"],
                             "summary": snapshot["summary"], "run_count": len(ids), "runs": page,
                             "next_run_after": page[-1]["id"] if start + limit < len(ids) else None})

    def _create_run(self, experiment_id, case_id, metadata):
        run_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, experiment_id, case_id, "pending", canonical(metadata), None, now(), None))
        return run_id

    def create_run(self, experiment_id, case_id, metadata):
        with self.lock, self.db:
            return self._create_run(experiment_id, case_id, metadata)

    def _raw_run(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise NotFound("运行不存在")
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        result["evaluation"] = json.loads(result["evaluation"]) if result["evaluation"] else None
        return result

    def run(self, run_id, force=False):
        with self.lock:
            result = self._raw_run(run_id)
            result["historical_evaluation"] = result["evaluation"]
            result["integrity"] = self.verify_run(run_id, force)
            latest = self.db.execute("SELECT * FROM evaluations WHERE run_id=? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
            if latest:
                result["evaluation"] = json.loads(latest["result"])
                result["evaluation_id"] = latest["id"]
            if result["integrity"]["state"] != "valid":
                result["evaluation"] = None
            return result

    def verify_run(self, run_id, force=False):
        with self.lock:
            run = self._raw_run(run_id)
            if run["status"] in ACTIVE:
                return {"state": "unverified", "reason": "execution_active", "checked_at": None}
            artifact = self.db.execute("SELECT * FROM artifacts WHERE run_id=? AND id='report'", (run_id,)).fetchone()
            path = confined(self.directory, artifact["path"]) if artifact else confined(self.directory, "runs/" + run_id + "/report.json")
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
            except OSError:
                signature = None
            cache_key = (self._revision("run", run_id),
                         signature, run["metadata"].get("events_sha256"), run["status"])
            cached = self.cache.get(run_id)
            if not force and cached and cached[0] == cache_key:
                return cached[1]
            state, reason, report_sha, event_sha = "valid", None, None, None
            malformed = False
            try:
                events = self.events(run_id)
                event_sha = trace_hash(events)
            except (LabError, ValueError, TypeError, KeyError):
                events, malformed = [], True
            seal = run["metadata"].get("events_sha256")
            if malformed:
                state, reason = "invalid", "invalid_trace"
            elif not seal:
                state, reason = "unverified", "trace_not_sealed"
            elif seal != event_sha:
                state, reason = "invalid", "trace_hash_mismatch"
            commits = [e for e in events if e["kind"] == "artifact_committed"]
            try:
                if artifact:
                    self.report(run_id)
                    report_sha = artifact["sha"]
                    if not malformed and (len(commits) != 1 or commits[0]["payload"].get("sha256") != report_sha):
                        raise LabError("artifact_trace_mismatch", "产物索引与提交事件不一致")
                elif signature is not None:
                    raise LabError("orphan_artifact", "报告文件没有可核查的提交索引")
                elif commits or run["status"] == "completed":
                    raise LabError("missing_artifact", "已记录提交或完成状态，但没有报告")
            except LabError as error:
                state, reason = "invalid", error.code
            old = self.db.execute("SELECT * FROM integrity WHERE run_id=?", (run_id,)).fetchone()
            result = {"state": state, "reason": reason, "checked_at": now(), "artifact_sha": report_sha, "trace_sha": event_sha}
            changes_before = self.db.total_changes
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO integrity VALUES(?,?,?,?,?,?)",
                                (run_id, state, reason, result["checked_at"], report_sha, event_sha))
                if state != "valid" and (not old or old["state"] != state or old["reason"] != reason):
                    self._audit(run_id, "integrity_check", result)
            self.validation_writes += self.db.total_changes - changes_before
            if force and (not cached or any(cached[1].get(field) != result.get(field)
                                           for field in ("state", "reason", "artifact_sha", "trace_sha"))):
                self.experiment_cache.pop(run["experiment_id"], None)
            cache_key = (self._revision("run", run_id),
                         signature, seal, run["status"])
            self.cache[run_id] = (cache_key, result)
            return result

    def claim(self, run_id):
        with self.lock, self.db:
            return self.db.execute("UPDATE runs SET status='running',ended_at=NULL WHERE id=? AND status='pending'", (run_id,)).rowcount == 1

    def update_metadata(self, run_id, metadata):
        with self.lock, self.db:
            self.db.execute("UPDATE runs SET metadata=? WHERE id=?", (canonical(metadata), run_id))

    def finish(self, run_id, status, evaluation=None, metadata=None):
        if status not in ACTIVE | TERMINAL:
            raise ValueError("未知运行状态")
        with self.lock, self.db:
            run = self._raw_run(run_id)
            if run["status"] in TERMINAL and run["status"] != status:
                raise LabError("invalid_transition", "已结束运行不能重新进入其他执行状态")
            if run["status"] == "running" and status == "pending":
                raise LabError("invalid_transition", "运行不能返回排队状态")
            if metadata is not None:
                self.db.execute("UPDATE runs SET metadata=? WHERE id=?", (canonical(metadata), run_id))
            self.db.execute("UPDATE runs SET status=?,evaluation=?,ended_at=? WHERE id=?",
                            (status, canonical(evaluation) if evaluation is not None else None,
                             now() if status in TERMINAL else None, run_id))

    def cancel_pending(self, run_id):
        with self.lock, self.db:
            run = self._raw_run(run_id)
            if run["status"] != "pending":
                return False
            self._event(run_id, "cancelled", {"reason": "user_request"})
            metadata = dict(run["metadata"], events_sha256=trace_hash(self.events(run_id)))
            self.db.execute("UPDATE runs SET status='cancelled',metadata=?,ended_at=? WHERE id=? AND status='pending'",
                            (canonical(metadata), now(), run_id))
            return True

    def _event(self, run_id, kind, payload):
        seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (run_id, seq, kind, canonical(payload), now()))
        return seq

    def event(self, run_id, kind, payload):
        with self.lock, self.db:
            return self._event(run_id, kind, payload)

    def events(self, run_id, after_seq=0, limit=None, tolerant=False, preview=False):
        with self.lock:
            sql = "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq"
            params = [run_id, after_seq]
            if limit is not None:
                if not 1 <= limit <= 200:
                    raise ValueError("每页事件数量应为 1—200")
                sql += " LIMIT ?"
                params.append(limit)
            result = []
            source_revision = self._revision("run", run_id) if preview else None
            for row in self.db.execute(sql, params).fetchall():
                try:
                    if preview:
                        _, raw, sha = self._payload_text(run_id, row["seq"], row["payload"], source_revision)
                        event = {"seq": row["seq"], "kind": row["kind"], "created_at": row["created_at"],
                                 "payload_preview": raw[:512], "payload_chars": len(raw), "payload_sha256": sha,
                                 "payload_truncated": len(raw) > 512}
                    else:
                        payload = json.loads(row["payload"])
                        if not isinstance(payload, dict):
                            raise ValueError("事件载荷应为对象")
                        event = {"seq": row["seq"], "kind": row["kind"], "payload": payload, "created_at": row["created_at"]}
                except (ValueError, TypeError):
                    if not tolerant:
                        raise LabError("invalid_trace", f"事件 #{row['seq']} 的记录格式损坏") from None
                    event = {"seq": row["seq"], "kind": "event_corrupt", "created_at": row["created_at"],
                             "payload": {"code": "invalid_trace", "original_kind": row["kind"],
                                         "raw_sha256": content_hash(row["payload"]),
                                         "message": "原事件载荷损坏；仅显示诊断，不作为有效执行证据"}}
                result.append(event)
            return result

    def _payload_text(self, run_id, seq, supplied=None, source_revision=None):
        revision = self._revision("run", run_id)
        current_source = supplied is None or source_revision == revision
        identity = (run_id, seq)
        cached = self.payload_cache.get(identity)
        if current_source and cached and cached[0] == revision:
            self.payload_cache.move_to_end(identity)
            self.payload_cache_hits += 1
            return cached[:3]
        self.payload_cache_misses += 1
        if cached:
            self.payload_cache_bytes -= self.payload_cache.pop(identity)[3]
        if supplied is None:
            row = self.db.execute("SELECT payload FROM events WHERE run_id=? AND seq=?", (run_id, seq)).fetchone()
            if not row:
                raise NotFound("事件不存在")
            supplied = row["payload"]
        if not isinstance(json.loads(supplied), dict):
            raise ValueError("事件载荷应为对象")
        sha = content_hash(supplied)
        # Python can use four bytes per code point. Bound retained text storage,
        # conservatively counting at least UTF-8 bytes and the worst string width.
        size = max(len(supplied.encode("utf-8")), len(supplied) * 4) + 256
        if current_source and size <= PAYLOAD_CACHE_BYTES and revision == self._revision("run", run_id):
            self.payload_cache[identity] = (revision, supplied, sha, size)
            self.payload_cache_bytes += size
            while len(self.payload_cache) > PAYLOAD_CACHE_ENTRIES or self.payload_cache_bytes > PAYLOAD_CACHE_BYTES:
                self.payload_cache_bytes -= self.payload_cache.popitem(last=False)[1][3]
        return revision, supplied, sha

    def event_payload(self, run_id, seq, sha256, offset=0, limit=4096):
        if seq < 1 or offset < 0 or not 1 <= limit <= 4096:
            raise ValueError("事件序号、偏移或分段大小无效")
        with self.lock:
            try:
                _, raw, actual = self._payload_text(run_id, seq)
            except NotFound:
                raise
            except (ValueError, TypeError):
                raise LabError("invalid_trace", "事件载荷损坏，仅可查看诊断") from None
            if actual != sha256:
                raise LabError("payload_changed", "事件载荷已变化，请刷新运行后重新查看")
            if offset > len(raw):
                raise ValueError("偏移超出事件载荷范围")
            end = min(offset + limit, len(raw))
            return {"seq": seq, "sha256": actual, "offset": offset, "next_offset": end if end < len(raw) else None,
                    "total_chars": len(raw), "text": raw[offset:end]}

    def _commit_index(self, run_id, key, sha, path):
        with self.db:
            existing = self.db.execute("SELECT * FROM artifacts WHERE run_id=?", (run_id,)).fetchone()
            if not existing:
                self.db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)", (run_id, "report", key, sha, path))
                self._event(run_id, "artifact_committed", {"artifact_id": "report", "sha256": sha})
            elif existing["sha"] != sha or existing["key"] != key:
                raise LabError("idempotency_conflict", "中断提交与现有产物不一致")
            self.db.execute("UPDATE commits SET state='committed' WHERE run_id=?", (run_id,))

    def _stage(self, stage):
        if self.commit_hook:
            self.commit_hook(stage)

    def save_report(self, run_id, report, key):
        if not isinstance(key, str) or not key or len(key) > 200:
            raise LabError("invalid_key", "幂等键无效")
        text, sha = canonical(report), digest(report)
        with self.lock:
            existing = self.db.execute("SELECT * FROM artifacts WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                if existing["key"] != key or existing["sha"] != sha:
                    raise LabError("idempotency_conflict", "报告已使用不同键或内容提交")
                self.report(run_id)
                return {"artifact_id": "report", "sha256": sha, "reused": True}
            intent = self.db.execute("SELECT * FROM commits WHERE run_id=?", (run_id,)).fetchone()
            if intent and (intent["key"] != key or intent["sha"] != sha):
                raise LabError("idempotency_conflict", "已有不同内容的提交意图")
            folder = confined(self.directory, "runs/" + run_id)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "report.json"
            relative = str(path.relative_to(self.directory))
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO commits VALUES(?,?,?,?,?)", (run_id, key, sha, relative, "pending"))
            self._stage("intent")
            temp = folder / "report.json.tmp"
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            self._stage("temp_written")
            os.replace(temp, path)
            self._stage("replaced")
            self._commit_index(run_id, key, sha, relative)
            self._stage("indexed")
            self._render_markdown(path, report)
            return {"artifact_id": "report", "sha256": sha, "reused": False}

    def _render_markdown(self, path, report):
        text = "# 发布核查报告\n\n状态：" + report["overall_status"] + "\n\n版本：" + str(report["release_version"]) + "\n\n"
        text += "\n".join("- " + item["text"] for item in report["features"] or [])
        text += "\n\n结构化报告（含引用）：\n\n~~~json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n~~~\n"
        try:
            path.with_suffix(".md").write_text(text, encoding="utf-8")
        except OSError:
            pass  # The display file is derived; JSON is authoritative.

    def report(self, run_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM artifacts WHERE run_id=? AND id='report'", (run_id,)).fetchone()
            if not row:
                raise LabError("missing_artifact", "没有已提交报告")
            path = confined(self.directory, row["path"])
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as error:
                raise LabError("missing_artifact", "报告文件丢失") from error
            if content_hash(text) != row["sha"]:
                raise LabError("artifact_hash_mismatch", "报告哈希不一致")
            try:
                return json.loads(text)
            except ValueError as error:
                raise LabError("invalid_artifact", "报告不是合法 JSON") from error

    def optional_report(self, run_id):
        if not self.db.execute("SELECT 1 FROM artifacts WHERE run_id=?", (run_id,)).fetchone():
            return None
        return self.report(run_id)

    def add_evaluation(self, run_id, result, input_hash):
        with self.lock, self.db:
            cursor = self.db.execute("INSERT INTO evaluations(run_id,version,result,input_hash,created_at) VALUES(?,?,?,?,?)",
                                     (run_id, result["version"], canonical(result), input_hash, now()))
            self._audit(run_id, "reevaluated", {"evaluation_id": cursor.lastrowid, "version": result["version"], "input_hash": input_hash})
            return cursor.lastrowid

    def evaluation_history(self, run_id):
        with self.lock:
            original = self._raw_run(run_id)["evaluation"]
            history = [{"id": None, "historical": True, "result": original}] if original else []
            return history + [dict(r, result=json.loads(r["result"])) for r in self.db.execute(
                "SELECT * FROM evaluations WHERE run_id=? ORDER BY id", (run_id,))]

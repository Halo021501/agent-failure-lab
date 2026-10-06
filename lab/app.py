from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .common import ROOT, LabError, NotFound, content_hash
from .compare import compare, summary
from .exporting import csv_export
from .runner import Runner
from .schemas import configured_budget
from .storage import Store
from .model_config import load_model_config, save_model_config


class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_ids: list[str] | None = None
    strategy: str = "recovery"
    repeats: StrictInt = Field(default=1, ge=1, le=10)
    seed: StrictInt = 0
    budget: dict | None = None
    agent_kind: str = "script"


class ReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = "trace_replay"
    strategy: str | None = None


class CompareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str
    right: str


def create_app(directory=ROOT / "data/runtime"):
    store = Store(Path(directory))
    runner = Runner(store)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lab-runner")

    @asynccontextmanager
    async def lifespan(app):
        yield
        with runner.lock:
            for token in runner.cancellations.values():
                token.set()
        executor.shutdown(wait=True, cancel_futures=True)
        store.close()

    app = FastAPI(title="Agent 故障演练场", lifespan=lifespan)
    app.state.store, app.state.runner, app.state.executor = store, runner, executor

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.url.hostname not in ("127.0.0.1", "localhost"):
            return JSONResponse({"detail": "仅允许本机访问", "code": "local_host_required"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "请求来源与本地服务不匹配", "code": "origin_mismatch"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(NotFound)
    async def not_found(request, error):
        return JSONResponse({"detail": str(error), "code": "not_found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid_input(request, error):
        return JSONResponse({"detail": str(error), "code": "invalid_input"}, status_code=400)

    @app.exception_handler(LabError)
    async def lab_error(request, error):
        return JSONResponse({"detail": str(error), "code": error.code}, status_code=409)

    def info(experiment_id, limit=50, after=None):
        result = store.experiment_info(experiment_id)
        result["summary"] = summary(result["runs"])
        result["run_count"] = len(result["runs"])
        start = 0
        if after:
            ids = [r["id"] for r in result["runs"]]
            if after not in ids:
                raise NotFound("运行游标不属于本实验")
            start = ids.index(after) + 1
        all_runs = result["runs"]
        result["runs"] = all_runs[start:start + limit]
        result["next_run_after"] = result["runs"][-1]["id"] if start + limit < len(all_runs) else None
        return result

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "0.3.0-dev"}

    @app.get("/api/status")
    def status():
        return {"version": "0.3.0-dev", "model_configured": runner.configured_model().public()["configured"], "agent_kind": "script",
                "suite": runner.suite.version, "suite_hash": runner.suite.hash,
                "case_count": len(runner.suite.cases), "queue_pending": store.pending_count(),
                "defaults": configured_budget().to_dict()}

    @app.get("/api/model-config")
    def model_config():
        return load_model_config().public()

    @app.post("/api/model-config")
    def configure_model(payload: dict):
        return save_model_config(payload)

    @app.get("/api/cases")
    def cases():
        return runner.suite.info()

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str):
        if case_id not in runner.suite.cases:
            raise NotFound("案例不存在")
        case = runner.suite.get(case_id)
        return dict(case.public(), faults=case.definition["faults"], case_hash=case.case_hash)

    @app.get("/api/experiments")
    def experiments(limit: int = Query(50, ge=1, le=100), before: str | None = None):
        return store.experiments(limit, before)

    @app.post("/api/experiments", status_code=202)
    def create_experiment(payload: ExperimentInput):
        experiment_id = runner.create_experiment(payload.case_ids, payload.strategy, payload.repeats, payload.seed,
                                                configured_budget(payload.budget), payload.agent_kind)
        executor.submit(runner.execute_experiment, experiment_id)
        return info(experiment_id)

    @app.get("/api/experiments/{experiment_id}")
    def experiment(experiment_id: str, limit: int = Query(50, ge=1, le=100), after: str | None = None):
        return info(experiment_id, limit, after)

    @app.post("/api/experiments/{experiment_id}/cancel")
    def cancel_experiment(experiment_id: str):
        for run in store.experiment_info(experiment_id)["runs"]:
            if run["status"] in ("pending", "running"):
                runner.cancel(run["id"])
        return info(experiment_id)

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        result = store.run(run_id, force=True)
        try:
            result["report"] = store.report(run_id)
        except LabError as error:
            result["report"] = None
            result["artifact_error"] = error.code
        result["evaluation_history"] = store.evaluation_history(run_id)
        result["audits"] = store.audits(run_id)[-100:]
        return result

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        store.run(run_id)
        return store.events(run_id, after_seq, limit)

    @app.get("/api/runs/{run_id}/evidence")
    def evidence(run_id: str, source_id: str, sha256: str):
        run = store.run(run_id, force=True)
        for event in store.events(run_id):
            if event["kind"] != "tool_result":
                continue
            result = event["payload"].get("result", {})
            if result.get("source_id") == source_id and result.get("content_sha256") == sha256 and "content" in result:
                if content_hash(result["content"]) != sha256:
                    raise LabError("source_hash_mismatch", "记录的来源内容哈希不一致")
                return dict(result, event_seq=event["seq"], call_id=event["payload"]["call_id"], integrity=run["integrity"])
        raise NotFound("未找到本次实际读取的来源证据")

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str):
        return runner.cancel(run_id)

    @app.post("/api/runs/{run_id}/replay", status_code=202)
    def replay(run_id: str, payload: ReplayInput):
        experiment_id, replay_id = runner.prepare_replay(run_id, payload.mode, payload.strategy)
        executor.submit(runner.execute_experiment, experiment_id)
        return {"experiment_id": experiment_id, "run_id": replay_id, "status": "pending"}

    @app.post("/api/runs/{run_id}/reevaluate")
    def reevaluate(run_id: str):
        return runner.reevaluate(run_id)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(run_id: str, artifact_id: str):
        run = store.run(run_id, force=True)
        if artifact_id != "report":
            raise NotFound("产物不存在")
        if run["integrity"]["state"] == "invalid":
            raise LabError(run["integrity"]["reason"], "记录完整性无效，报告不能作为有效实验产物")
        return store.report(run_id)

    @app.post("/api/compare")
    def compare_experiments(payload: CompareInput):
        return compare(store.experiment_info(payload.left, force=True), store.experiment_info(payload.right, force=True))

    @app.get("/api/experiments/{experiment_id}/export")
    def export(experiment_id: str, format: str = "json"):
        result = store.experiment_info(experiment_id, force=True)
        result["summary"] = summary(result["runs"])
        if format == "json":
            return JSONResponse(result, headers={"Content-Disposition": f'attachment; filename="{experiment_id}.json"'})
        if format != "csv":
            raise ValueError("导出格式应为 json 或 csv")
        return Response(csv_export(result), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{experiment_id}.csv"'})

    return app

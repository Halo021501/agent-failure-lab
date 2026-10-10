from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Query, Path as ApiPath
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .common import ROOT, LabError, NotFound, content_hash
from . import __version__
from .cases import Suite
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
    suite_namespace: str | None = None


class ReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = "trace_replay"
    strategy: str | None = None


class CompareInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    left: str
    right: str


def create_app(directory=ROOT / "data/runtime", namespace="v1"):
    store = Store(Path(directory))
    runner = Runner(store, Suite(namespace=namespace))
    runners = {namespace: runner}
    for name in ("v1", "heldout-v1"):
        if name != namespace:
            sibling = Runner(store, Suite(namespace=name))
            sibling.cancellations, sibling.lock = runner.cancellations, runner.lock
            runners[name] = sibling
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
    app.state.runners = runners

    def suite_runner(name=None):
        name = namespace if name is None else name
        if name not in runners:
            raise NotFound("套件不存在")
        return runners[name]

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
        return store.experiment_page(experiment_id, limit, after)

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/status")
    def status():
        return {"version": __version__, "model_configured": runner.configured_model().public()["configured"], "agent_kind": "script",
                "suite_namespace": namespace, "capabilities": ["suite_selection", "corrupt_event_diagnostics", "event_payload_chunks", "experiment_snapshot_cache"],
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
    def cases(suite: str | None = None):
        return suite_runner(suite).suite.info()

    @app.get("/api/suites")
    def suites():
        return {"default": namespace, "suites": [{key: value for key, value in item.suite.info().items() if key != "cases"}
                                                 for item in runners.values()]}

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str, suite: str | None = None):
        selected = suite_runner(suite)
        if case_id not in selected.suite.cases:
            raise NotFound("案例不存在")
        case = selected.suite.get(case_id)
        return dict(case.public(), faults=case.definition["faults"], case_hash=case.case_hash)

    @app.get("/api/experiments")
    def experiments(limit: int = Query(50, ge=1, le=100), before: str | None = None):
        return store.experiments(limit, before)

    @app.post("/api/experiments", status_code=202)
    def create_experiment(payload: ExperimentInput):
        selected = suite_runner(payload.suite_namespace)
        experiment_id = selected.create_experiment(payload.case_ids, payload.strategy, payload.repeats, payload.seed,
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
    def events(run_id: str, after_seq: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200), preview: bool = False):
        store.run(run_id)
        return store.events(run_id, after_seq, limit, tolerant=True, preview=preview)

    @app.get("/api/runs/{run_id}/events/{seq}/payload")
    def event_payload(run_id: str, seq: int = ApiPath(ge=1), sha256: str = Query(pattern="^[0-9a-f]{64}$"),
                      offset: int = Query(0, ge=0), limit: int = Query(4096, ge=1, le=4096)):
        run = store.run(run_id)
        if run["integrity"]["state"] == "invalid":
            raise LabError(run["integrity"]["reason"], "记录完整性异常，仅可查看事件预览与诊断")
        return store.event_payload(run_id, seq, sha256, offset, limit)

    @app.get("/api/runs/{run_id}/evidence")
    def evidence(run_id: str, source_id: str, sha256: str):
        run = store.run(run_id, force=True)
        if run["integrity"]["state"] == "invalid":
            raise LabError(run["integrity"]["reason"], "记录完整性异常，不能提供有效引用证据")
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
        source = store.run(run_id)
        selected = suite_runner(source["metadata"].get("suite_namespace", "v1"))
        experiment_id, replay_id = selected.prepare_replay(run_id, payload.mode, payload.strategy)
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

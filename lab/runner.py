import copy
import threading
import time
from .agents.baseline import Baseline, STRATEGIES
from .cases import Suite
from .common import ROOT, digest, Halt, LabError
from .evaluate import evaluate, VERSION, expected_counts
from .gateway import Gateway
from .provenance import fingerprints, evaluation_input
from .replay import recorded_calls, trace_replay, normalized_events
from .schemas import Budget, configured_budget
from .storage import now
from .model_config import ModelConfig, load_model_config
from .model_protocol import STRATEGIES as MODEL_STRATEGIES, VERSION as PROMPT_VERSION, prompt_fingerprint
from .model_transport import OpenAITransport, VERSION as TRANSPORT_VERSION

MODEL_STATS = ("model_calls", "network_requests", "token_count_requests", "input_tokens", "output_tokens",
               "usage_unknown_calls", "reserved_input_tokens", "reserved_output_tokens")


def source_fingerprint():
    return fingerprints()["source_fingerprint"]


class Runner:
    def __init__(self, store, suite=None, model_config=None, transport_factory=None):
        self.store, self.suite = store, suite or Suite()
        self.cancellations = {}
        self.lock = threading.Lock()
        self.provenance = fingerprints()
        self.fingerprint = self.provenance["source_fingerprint"]
        self.model_config = model_config
        self.transport_factory = transport_factory or OpenAITransport

    def configured_model(self):
        return self.model_config or load_model_config()

    def _model_fields(self, agent_kind, strategy, config):
        if agent_kind == "script":
            return {}
        return {"model_config": config.to_dict(), "provider": config.provider, "model": config.model,
                "prompt_version": PROMPT_VERSION, "prompt_fingerprint": prompt_fingerprint(strategy),
                "transport_version": TRANSPORT_VERSION}

    def _config(self, case_ids, strategy, seeds, budget, mode="fresh_run"):
        return {"schema_version": 2, "suite_version": self.suite.version, "suite_hash": self.suite.hash,
                "suite_namespace": self.suite.namespace,
                "case_ids": sorted(case_ids), "strategy": strategy, "agent_kind": "script",
                "seeds": seeds, "budget": budget.to_dict(), "mode": mode,
                "evaluator_version": VERSION, "comparison_axis": "strategy", **self.provenance}

    def _metadata(self, case, strategy, seed, budget, mode="fresh_run"):
        return {"schema_version": 2, "mode": mode, "agent_kind": "script", "strategy": strategy,
                "strategy_version": Baseline.version, "evaluator_version": VERSION,
                "case_hash": case.case_hash, "suite_hash": self.suite.hash,
                "suite_namespace": self.suite.namespace,
                "suite_snapshot": True,
                **self.provenance, "seed": seed, "budget": budget.to_dict(),
                "recoverable": case.definition["recoverable"],
                "fault_ids": [f["id"] for f in case.definition["faults"]],
                "activated_faults": [], "tool_calls": 0, "model_calls": 0,
                "expected_counts": expected_counts(case.expected)}

    def create_experiment(self, case_ids=None, strategy="recovery", repeats=1, seed=0, budget=None, agent_kind="script"):
        strategies = STRATEGIES if agent_kind == "script" else MODEL_STRATEGIES if agent_kind == "model" else ()
        if strategy not in strategies or type(repeats) is not int or not 1 <= repeats <= 10 or type(seed) is not int:
            raise ValueError("策略、重复次数或种子无效")
        config_model = self.configured_model() if agent_kind == "model" else None
        if config_model:
            config_model.require_ready()
        budget = budget or configured_budget()
        case_ids = case_ids if case_ids is not None else list(self.suite.cases)
        if not case_ids or len(case_ids) != len(set(case_ids)):
            raise ValueError("请选择非空且不重复的案例")
        cases = [self.suite.get(case_id) for case_id in sorted(case_ids)]
        seeds = list(range(seed, seed + repeats))
        self.suite.freeze(self.store.directory)
        config = self._config(case_ids, strategy, seeds, budget)
        model_fields = self._model_fields(agent_kind, strategy, config_model)
        config.update(agent_kind=agent_kind, comparison_axis="prompt" if agent_kind == "model" else "strategy", **model_fields)
        specs = [(case.id, self._metadata(case, strategy, run_seed, budget)) for case in cases for run_seed in seeds]
        for _, metadata in specs:
            metadata.update(agent_kind=agent_kind, strategy_version=PROMPT_VERSION if agent_kind == "model" else Baseline.version, **model_fields)
            if agent_kind == "model":
                metadata.update({name: 0 for name in MODEL_STATS})
        experiment_id, _ = self.store.create_batch(config, specs)
        return experiment_id

    def execute_experiment(self, experiment_id):
        for run in self.store.experiment_info(experiment_id)["runs"]:
            if run["status"] == "pending":
                self.execute_run(run["id"])
        return self.store.experiment_info(experiment_id)

    def execute_run(self, run_id, on_call=None):
        run = self.store.run(run_id)
        with self.lock:
            if run["status"] != "pending":
                return run
            cancel = self.cancellations.setdefault(run_id, threading.Event())
            if cancel.is_set():
                self.store.cancel_pending(run_id)
                self.cancellations.pop(run_id, None)
                return self.store.run(run_id)
            if not self.store.claim(run_id):
                self.cancellations.pop(run_id, None)
                return self.store.run(run_id)
        metadata = copy.deepcopy(run["metadata"])
        started = time.perf_counter()
        gateway = None
        status, evaluation, error_code = "completed", None, None
        try:
            if metadata["mode"] == "trace_replay":
                if cancel.is_set():
                    raise Halt("cancelled", "回放已取消")
                trace_replay(self.store, metadata["parent_run_id"], run["experiment_id"], run_id)
                return self.store.run(run_id)
            case = self._run_suite(metadata).get(run["case_id"])
            budget = Budget(**metadata["budget"])
            replay_calls = recorded_calls(self.store.events(metadata["parent_run_id"])) if metadata["mode"] == "tool_replay" else None
            metadata["started_at"] = now()
            self.store.update_metadata(run_id, metadata)
            self.store.event(run_id, "run_started", {"case_id": case.id, "strategy": metadata["strategy"], "mode": metadata["mode"]})

            def progress(g):
                metadata.update(tool_calls=g.tool_calls, current_step="tool:" + str(g.tool_calls),
                                activated_faults=sorted(g.faults.activated),
                                wall_seconds=time.perf_counter() - g.started)
                self.store.update_metadata(run_id, metadata)
                if on_call:
                    on_call(g)

            gateway = Gateway(case, self.store, run_id, budget, cancel, metadata["seed"], replay_calls, progress)
            gateway.started = started  # Include snapshot validation and initialization in the run's wall budget.
            gateway.check()
            if metadata.get("agent_kind") == "model":
                from .agents.model import ModelAgent
                model_config = ModelConfig(**metadata["model_config"])
                model_config.require_ready()
                stats = {name: metadata.get(name, 0) for name in MODEL_STATS}

                def model_progress(step):
                    metadata.update(stats, current_step=step, wall_seconds=time.perf_counter() - gateway.started)
                    self.store.update_metadata(run_id, metadata)

                try:
                    ModelAgent(metadata["strategy"], model_config, self.transport_factory(model_config), stats,
                               model_progress).execute(gateway, case.definition["target_version"])
                finally:
                    metadata.update(stats)
            else:
                Baseline(metadata["strategy"], budget).execute(gateway, case.definition["target_version"])
            gateway.check()
            if replay_calls is not None and gateway.replay_position != len(replay_calls):
                raise Halt("replay_mismatch", "录制中仍有未消费的调用")
            report = self.store.report(run_id)
            evaluation = evaluate(case, report, self.store.events(run_id))
            evaluation["evaluator_fingerprint"] = self.provenance["evaluator_fingerprint"]
        except Halt as error:
            error_code = error.code
            status = error.code if error.code in ("cancelled", "budget_exceeded") else "failed"
        except (LabError, ValueError) as error:
            status, error_code = "failed", getattr(error, "code", "invalid_data")
            self.store.event(run_id, "run_error", {"code": error_code, "message": str(error)})
        except Exception as error:
            status, error_code = "failed", "internal_error"
            self.store.event(run_id, "run_error", {"code": error_code, "message": type(error).__name__ + ": " + str(error)})
        finally:
            if metadata["mode"] == "trace_replay" and status == "completed":
                with self.lock:
                    self.cancellations.pop(run_id, None)
        if gateway:
            # A lost acknowledgement can leave a valid report behind. Assess its
            # facts independently of execution success; do not call present facts missing.
            if status in ("failed", "budget_exceeded"):
                try:
                    committed = self.store.optional_report(run_id)
                    if committed is not None:
                        evaluation = evaluate(case, committed, self.store.events(run_id))
                        evaluation.update(contract_pass=False, complete_success=False,
                                          evaluator_fingerprint=self.provenance["evaluator_fingerprint"])
                        evaluation["checks"].append({"name":"execution_completed", "passed":False, "detail":status})
                except (LabError, ValueError, TypeError, KeyError):
                    evaluation = None
            metadata.update(tool_calls=gateway.tool_calls, model_calls=metadata.get("model_calls", 0),
                            executed_tool_calls=0 if gateway.replay_calls is not None else gateway.tool_calls,
                            executed_model_calls=metadata.get("model_calls", 0), activated_faults=sorted(gateway.faults.activated),
                            retries=sum(max(0, count - 1) for count in gateway.counts.values()),
                            simulated_fault_seconds=gateway.simulated_seconds)
        metadata.update(wall_seconds=time.perf_counter() - started, error_code=error_code, current_step="finished")
        self.store.event(run_id, "run_finished", {"status": status, "error_code": error_code})
        metadata["events_sha256"] = digest(normalized_events(self.store.events(run_id)))
        self.store.finish(run_id, status, evaluation, metadata)
        with self.lock:
            self.cancellations.pop(run_id, None)
        return self.store.run(run_id)

    def cancel(self, run_id):
        with self.lock:
            run = self.store.run(run_id)
            if run["status"] in ("pending", "running"):
                token = self.cancellations.setdefault(run_id, threading.Event())
                token.set()
                if self.store.cancel_pending(run_id):
                    self.cancellations.pop(run_id, None)
        return self.store.run(run_id)

    def _run_suite(self, metadata):
        archive = self.store.directory / "suites" / metadata["suite_hash"]
        if archive.exists():
            suite = Suite(archive, metadata.get("suite_namespace", "v1"))
            if suite.hash != metadata["suite_hash"]:
                raise LabError("suite_snapshot_mismatch", "原资料快照完整性异常")
            return suite
        if metadata.get("suite_snapshot"):
            raise LabError("suite_snapshot_missing", "原资料快照丢失")
        current = Suite(self.suite.root, metadata.get("suite_namespace", "v1"))
        if current.hash != metadata["suite_hash"]:
            raise LabError("suite_mismatch", "找不到原套件资料快照")
        return current

    def prepare_replay(self, source_id, mode="trace_replay", strategy=None):
        if mode not in ("trace_replay", "tool_replay", "fresh_run"):
            raise ValueError("未知回放模式")
        source = self.store.run(source_id, force=True)
        if source["status"] in ("pending", "running"):
            raise ValueError("运行尚未结束")
        chosen = strategy or source["metadata"]["strategy"]
        agent_kind = source["metadata"].get("agent_kind", "script")
        if chosen not in (MODEL_STRATEGIES if agent_kind == "model" else STRATEGIES):
            raise ValueError("未知策略")
        if mode in ("trace_replay", "tool_replay") and source["integrity"]["state"] != "valid":
            raise LabError(source["integrity"]["reason"], "原记录完整性未通过，请检查证据")
        if mode == "fresh_run" and source["metadata"]["suite_hash"] != self.suite.hash:
            raise ValueError("当前套件与原记录不同")
        if mode == "tool_replay":
            recorded_calls(self.store.events(source_id))
        budget = Budget(**source["metadata"]["budget"])
        if mode == "trace_replay":
            config = dict(self.store.experiment_info(source["experiment_id"])["config"],
                          case_ids=[source["case_id"]], seeds=[source["metadata"]["seed"]], mode=mode)
            metadata = dict(source["metadata"], mode=mode, parent_run_id=source_id)
        else:
            suite = self._run_suite(source["metadata"]) if mode == "tool_replay" else self.suite
            if suite.hash != self.suite.hash:
                replay_runner = Runner(self.store, suite, self.model_config, self.transport_factory)
                replay_runner.cancellations, replay_runner.lock = self.cancellations, self.lock
                return replay_runner.prepare_replay(source_id, mode, chosen)
            self.suite.freeze(self.store.directory)
            case = suite.get(source["case_id"])
            config = self._config([case.id], chosen, [source["metadata"]["seed"]], budget, mode)
            metadata = dict(self._metadata(case, chosen, source["metadata"]["seed"], budget, mode), parent_run_id=source_id)
            model_config = self.configured_model() if agent_kind == "model" else None
            if model_config:
                model_config.require_ready()
            fields = self._model_fields(agent_kind, chosen, model_config)
            config.update(agent_kind=agent_kind, comparison_axis="prompt" if agent_kind == "model" else "strategy", **fields)
            metadata.update(agent_kind=agent_kind, strategy_version=PROMPT_VERSION if model_config else Baseline.version, **fields)
            if model_config:
                metadata.update({name: 0 for name in MODEL_STATS})
        experiment_id, ids = self.store.create_batch(config, [(source["case_id"], metadata)])
        return experiment_id, ids[0]

    def replay(self, source_id, mode="trace_replay", strategy=None):
        experiment_id, run_id = self.prepare_replay(source_id, mode, strategy)
        self.execute_run(run_id)
        return experiment_id, run_id

    def reevaluate(self, run_id):
        run = self.store.run(run_id, force=True)
        if run["status"] in ("pending", "running") or run["integrity"]["state"] != "valid":
            raise LabError("evaluation_unavailable", "仅可重新评测完整性有效且已结束的记录")
        case = self._run_suite(run["metadata"]).get(run["case_id"])
        report = self.store.optional_report(run_id)
        result = evaluate(case, report, self.store.events(run_id))
        result["evaluator_fingerprint"] = self.provenance["evaluator_fingerprint"]
        if run["status"] != "completed":
            result.update(contract_pass=False, complete_success=False)
            result["checks"].append({"name": "execution_completed", "passed": False, "detail": run["status"]})
        self.store.add_evaluation(run_id, result, evaluation_input(run, report))
        return self.store.run(run_id)

import argparse
import csv
import io
import json
from pathlib import Path
from .common import ROOT
from .cases import Suite
from .compare import compare, summary
from .runner import Runner
from .schemas import Budget
from .storage import Store
from .exporting import csv_export
from .model_config import load_model_config, save_model_config


def main():
    parser = argparse.ArgumentParser(description="Agent 故障演练场")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/runtime")
    parser.add_argument("--suite", choices=("v1", "heldout-v1"), default="v1")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("cases")
    settings = commands.add_parser("model-config")
    settings.add_argument("--model")
    settings.add_argument("--api-key-env")
    run = commands.add_parser("run")
    run.add_argument("--cases", nargs="+")
    run.add_argument("--agent", choices=("script", "model"), default="script")
    run.add_argument("--strategy", choices=("recovery", "no-recovery", "model-minimal", "model-recovery"))
    run.add_argument("--repeats", type=int, default=1)
    run.add_argument("--seed", type=int, default=0)
    replay = commands.add_parser("replay")
    replay.add_argument("run_id")
    replay.add_argument("--mode", choices=("trace_replay", "tool_replay", "fresh_run"), default="trace_replay")
    replay.add_argument("--strategy", choices=("recovery", "no-recovery", "model-minimal", "model-recovery"))
    reevaluate = commands.add_parser("reevaluate")
    reevaluate.add_argument("run_id")
    diff = commands.add_parser("compare")
    diff.add_argument("left")
    diff.add_argument("right")
    export = commands.add_parser("export")
    export.add_argument("experiment_id")
    export.add_argument("--format", choices=("json", "csv"), default="json")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    if args.command == "model-config":
        config = load_model_config()
        values = config.to_dict()
        if args.model is not None:
            values["model"] = args.model
        if args.api_key_env is not None:
            values["api_key_env"] = args.api_key_env
        result = save_model_config(values) if args.model is not None or args.api_key_env is not None else config.public()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "cases":
        print(json.dumps(Suite(namespace=args.suite).info(), ensure_ascii=False, indent=2))
        return
    if args.command == "serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(args.data_dir), host="127.0.0.1", port=args.port)
        return
    store = Store(args.data_dir)
    runner = Runner(store, Suite(namespace=args.suite))
    try:
        if args.command == "run":
            strategy = args.strategy or ("model-recovery" if args.agent == "model" else "recovery")
            experiment_id = runner.create_experiment(args.cases, strategy, args.repeats, args.seed, agent_kind=args.agent)
            result = runner.execute_experiment(experiment_id)
            print(json.dumps({"experiment_id": experiment_id, "summary": summary(result["runs"]),
                              "runs": [{"id": r["id"], "case_id": r["case_id"], "status": r["status"],
                                        "contract_pass": bool(r["evaluation"] and r["evaluation"]["contract_pass"]),
                                        "error_code": r["metadata"].get("error_code")} for r in result["runs"]]}, ensure_ascii=False, indent=2))
        elif args.command == "replay":
            experiment_id, run_id = runner.replay(args.run_id, args.mode, args.strategy)
            print(json.dumps({"experiment_id": experiment_id, "run": store.run(run_id)}, ensure_ascii=False, indent=2))
        elif args.command == "compare":
            print(json.dumps(compare(store.experiment_info(args.left, force=True), store.experiment_info(args.right, force=True)), ensure_ascii=False, indent=2))
        elif args.command == "reevaluate":
            print(json.dumps(runner.reevaluate(args.run_id), ensure_ascii=False, indent=2))
        elif args.command == "export":
            result = store.experiment_info(args.experiment_id, force=True)
            result["summary"] = summary(result["runs"])
            if args.format == "json":
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(csv_export(result), end="")
    finally:
        store.close()


if __name__ == "__main__":
    main()

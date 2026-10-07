# Agent Failure Lab

[中文](README.md) | **English**

**Check task outcomes, facts, and citations against execution records after tool failures.**

Updated: October 7, 2026 (Asia/Shanghai). The v0.2 offline improvements are implemented; the v0.3 OpenAI Responses adapter is undergoing development acceptance. At the user's request, paid real-model testing is deferred. External model requests: **0**. See the [build report](docs/BUILD_REPORT_V02_V03.md) for the verified scope and remaining work.

Source: [private GitHub repository](https://github.com/Halo021501/agent-failure-lab), default branch `main`. GitHub Actions is disabled; this push runs no cloud builds or tests. See [repository delivery notes](docs/REPOSITORY_DELIVERY.md). Future development follows the [project conventions](AGENTS.md): keep both READMEs in sync and maintain a bilingual repository description.

## Startup and local service

The existing local workspace has its own dependencies installed:

~~~powershell
Set-Location -LiteralPath 'E:\Fun projects\03-agent-failure-lab'
.\start.ps1
~~~

Open [Agent Failure Lab](http://127.0.0.1:8767). If a process already occupies the port, `start.ps1` will report it. Open the existing page, or press Ctrl+C in its service terminal before starting again. On a new machine, run `setup.ps1` first; dependencies are pinned in `requirements.lock.txt`. The local project directory retains the `03` prefix after the repository rename; a new GitHub clone can use the `agent-failure-lab` directory.

**At the last service check on October 6, 2026, port 8767 was running the v0.2 backend.** The new source and static pages are saved. That older service needs a restart to enable the v0.3 model configuration API. The model entry is disabled on the older backend and shows a restart message. Restarting the service does not itself make model requests.

## Implemented features

- The original 30 cases remain frozen, with two scripted strategies and explicit fault activation and retry records.
- Reports and execution events have separate hash checks. Integrity failures exclude results from current evaluation while preserving historical results and audit records.
- Commit intents, atomic file replacement, and transactional artifact indexing and commit events. Retrying the same key and content commits only once.
- Replay reproduces committed side effects when acknowledgements are lost. Failed runs without artifacts also support strict replay.
- Atomic queue capacity across the database, conditional claiming, cancellation race protection, valid terminal transitions, and live execution steps.
- Experiment and run pagination, incremental event loading, Chinese report rendering, collapsible JSON, source locations, and navigation to read events.
- Append-only reevaluation preserves original results. Individual facts, omissions, errors, citations, field groups, and fault-rule coverage are counted separately.
- Comparisons show no improvement labels when conditions differ or an experiment is incomplete. JSON, CSV, and CLI output share metric logic.
- An OpenAI Responses tool loop, minimal and bounded-recovery prompts, non-secret local configuration, and environment-variable credentials.
- Input-token preflight, output limits, per-run and batch budgets, and cooperative cancellation during requests. Errors or unknown usage stop further generation.
- Fixed source and separate evaluation ground-truth snapshots for each experiment. Another 12 independently authored synthetic held-out cases have not been used to tune model prompts.

All source materials are synthetic files created for this project. Public tools can only list the case index, read public materials for that case, and write its report. Evaluation ground truth and fault plans are not included in model requests.

## Usage and metrics

1. Select cases, a strategy, repetitions, and budgets, then start the experiment.
2. Open a run to inspect its Chinese report, integrity status, evaluation assertions, and timeline.
3. Click a citation to locate the actual source content read during that run, then jump to the read event.
4. Create a second experiment under matching conditions to compare individual cases or export results.

Complete success and contract pass are separate metrics. A correct `partial` report can pass its contract without counting as complete success. The contract-pass denominator includes only naturally terminated runs with valid integrity (`completed`, `failed`, or `budget_exceeded`). Runs canceled before starting are excluded from duration metrics. Planned, executed, and finished coverage are reported separately; cancellation or interruption prevents a formal whole-experiment comparison. Naturally terminated failures still count in fact and citation denominators. Facts in a committed report are evaluated as delivered, but the run is not counted as successful; only failures without a report count every expected fact as missing.

Facts include the version, each feature, each unresolved issue, and three test statistics. Valid empty lists have their own facts. Field groups remain version, features, issues, and tests. Required citations cover version, features, issues (including source evidence for an empty issue list), and the target test-data rows. Duplicate content cannot increase scores. Older v1 evaluations without these metrics show unknown values and can be reevaluated.

| Mode | Behavior |
| --- | --- |
| `trace_replay` | Verifies and copies sealed events, reports, and historical evaluations. Executes zero tools or model calls in this replay and is excluded from capability metrics. |
| `tool_replay` | The agent makes new decisions. Each tool and its arguments must exactly match the recording, with no fallback to live tools. A model agent makes new model requests and records new usage. |
| `fresh_run` | Executes from the beginning using fixed materials and the fault plan. The current suite must match the source conditions. |

The gateway rejects duplicate or stale deliveries. The agent chooses retries, backup sources, and confirmation of report acknowledgements. Scripted and simulated-provider results are used only for harness acceptance.

## CLI and free offline verification

~~~powershell
.\.venv\Scripts\python.exe -m lab cases
.\.venv\Scripts\python.exe -m lab run --cases N01 F-T01 F-M04
.\.venv\Scripts\python.exe -m lab run --strategy no-recovery
.\.venv\Scripts\python.exe -m lab replay <run_id> --mode trace_replay
.\.venv\Scripts\python.exe -m lab reevaluate <run_id>
.\.venv\Scripts\python.exe -m lab compare <experiment_A> <experiment_B>
.\.venv\Scripts\python.exe -m lab export <experiment_id> --format csv
.\.venv\Scripts\python.exe -m lab --suite heldout-v1 cases
.\.venv\Scripts\python.exe scripts/verify_core.py
.\.venv\Scripts\python.exe scripts/verify_offline.py
.\.venv\Scripts\python.exe scripts/plan_model_experiments.py
~~~

Place `--data-dir` and `--suite` before the subcommand. The web app currently defaults to the original v1 suite; use the CLI for the held-out suite. The model experiment planner only writes a plan and cannot make requests.

`verify_core` checks the core and simulated provider, writing machine-readable results to `docs/CORE_VERIFICATION.json`; it excludes HTTP and async transport tests. The full local check is `python -m unittest discover -s tests -q`. `verify_offline` creates a separate `data/verification/acceptance-*` directory, saves 180 scripted runs and 120 replays, and updates `docs/verification-summary-v0.3-offline.json`. Historical v0.1 and v0.2 summaries are preserved.

Final verification on October 6, 2026: **71 core and simulated-model tests passed, with 180 scripted runs and 120 replays verified.** Source fingerprints matched across the three verification and planning records. The recovery strategy passed the contract in 90/90 runs and achieved complete success in 75/90. These are scripted-harness results; external model requests remain at 0. Tests were not rerun for this repository rename and bilingual documentation update.

## Model configuration (real tests deferred)

`config/model.json` stores only the model name, environment-variable name, and budgets. It stores no key and selects no model by default. Non-secret settings can be changed through the web configuration or CLI:

~~~powershell
.\.venv\Scripts\python.exe -m lab model-config
.\.venv\Scripts\python.exe -m lab model-config --model <model_name>
~~~

When real-model execution is enabled in the future, set `OPENAI_API_KEY` in the terminal that starts the service, then restart it. Do not send the key in chat. Model execution requires explicitly selecting the model execution mode, or using CLI `run --agent model --strategy model-recovery`; the default is always `script`. Those model execution commands have not been run for this delivery.

See [model integration notes](docs/MODEL_INTEGRATION.md) for the adapter contract, usage, and cancellation limits. The 252 planned real runs have not started. Each prompt has 90 original-suite runs and 36 held-out runs; see the [machine-readable plan](docs/MODEL_EXPERIMENT_PLAN.json). Token caps are protective limits, not consumption or cost estimates.

## Data, acceptance boundaries, and references

SQLite is stored at `data/runtime/lab.db`, reports at `runs/<run_id>/`, and fixed source snapshots at `suites/<suite_hash>/` within the selected data directory. Use separate `--data-dir` values when running the CLI and web app concurrently; execution by multiple processes in the same directory is not supported. The first migration from schema v1 makes a consistent backup of the database and reports. Restarting preserves records and marks unfinished runs as `interrupted`; they do not resume automatically.

- Local file I/O supports cooperative cancellation at call boundaries and cannot forcibly interrupt a stuck system call.
- Canceling an external request stops local waiting, but the provider may still execute or charge for it. Budgets retain conservative reservations when usage is unavailable; cost remains unknown.
- If provider token usage exceeds the preflight or output cap, the actual value is recorded and execution stops. Local budgets do not replace provider account spending limits.
- Large responses are still stored in events. Pagination limits the number of returned records; large-content chunking is not implemented. Manually corrupted events may cause errors in some browsing views, but invalid results will not continue counting toward evaluation scores.
- An independent API performance script for 100+ experiments, 300 runs, and 10,000 events is written but has not been run. Current HTTP and async transport acceptance remains pending.
- Local tests cannot establish real-model capability. There is currently no evidence from real requests, actual model token consumption, or real charges.

Development references: [improvement plan](docs/NEXT_PHASE_PLAN.md), [task list](docs/NEXT_PHASE_TASKS.md), [current build report](docs/BUILD_REPORT_V02_V03.md), [original build report](docs/BUILD_REPORT.md), and [held-out freeze manifest](docs/HELDOUT_FREEZE.json). These detailed project documents are currently in Chinese.

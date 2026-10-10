# Agent Failure Lab

[中文](README.md) | **English**

**Check task outcomes, facts, and citations against execution records after tool failures.**

Updated: October 11, 2026 (Asia/Shanghai). v0.3.2-dev adds cached overviews for terminal experiments and on-demand event payload chunks. All 105 local checks passed. On the same fixed dataset, median warm reading of a 50-run overview page decreased from 214 ms to 97 ms. At the user's request, real-model testing remains deferred. External model requests: **0**. See the [iteration plan](docs/V032_PLAN.md) and [build report](docs/BUILD_REPORT_V032.md) for scope and verification evidence.

Source: [private GitHub repository](https://github.com/Halo021501/agent-failure-lab), default branch `main`. GitHub Actions is disabled; this push runs no cloud builds or tests. See [repository delivery notes](docs/REPOSITORY_DELIVERY.md). Future development follows the [project conventions](AGENTS.md): keep both READMEs in sync and maintain a bilingual repository description.

## Startup and local service

The existing local workspace has its own dependencies installed:

~~~powershell
Set-Location -LiteralPath 'E:\Fun projects\03-agent-failure-lab'
.\start.ps1
~~~

Open [Agent Failure Lab](http://127.0.0.1:8767). If a process already occupies the port, `start.ps1` will report it. Open the existing page, or press Ctrl+C in its service terminal before starting again. On a new machine, run `setup.ps1` first; dependencies are pinned in `requirements.lock.txt`. The local project directory retains the `03` prefix after the repository rename; a new GitHub clone can use the `agent-failure-lab` directory.

**The local v0.3.2-dev service was started and checked on October 11, 2026.** Select the original or held-out suite, expand events to browse payload chunks, and view non-secret model configuration. The footer shows the actual service version; restart after subsequent Python source changes. Restarting does not itself make model requests.

## Implemented features

- The original 30 cases and 12 independent held-out cases remain frozen. Select either suite in the web app, with two scripted strategies and explicit fault activation and retry records.
- Reports and execution events have separate hash checks. Integrity failures exclude results from current evaluation while preserving historical results and audit records.
- Commit intents, atomic file replacement, and transactional artifact indexing and commit events. Retrying the same key and content commits only once.
- Replay reproduces committed side effects when acknowledgements are lost. Failed runs without artifacts also support strict replay.
- Atomic queue capacity across the database, conditional claiming, cancellation race protection, valid terminal transitions, and live execution steps.
- Experiment and run pagination, incremental event loading, Chinese report rendering, collapsible JSON, source locations, and navigation to read events.
- Terminal experiments reuse a bounded overview cache. Database revisions or report file-state changes invalidate it; exports and comparisons still force integrity checks.
- Web events initially return 512-character previews. Expanding loads up to 4096 Unicode code points per chunk; previous/next controls replace the current text. SHA256 prevents mixing payload snapshots. The original full-event API remains compatible.
- Corrupt events appear as diagnostic placeholders with their sequence and raw-payload hash. Strict evaluation, replay, and evidence endpoints reject corrupt records without changing original events.
- Append-only reevaluation preserves original results. Individual facts, omissions, errors, citations, field groups, and fault-rule coverage are counted separately.
- Comparisons show no improvement labels when conditions differ or an experiment is incomplete. JSON, CSV, and CLI output share metric logic.
- An OpenAI Responses tool loop, minimal and bounded-recovery prompts, non-secret local configuration, and environment-variable credentials.
- Input-token preflight, output limits, per-run and batch budgets, and cooperative cancellation during requests. Client initialization counts toward deadlines; cancellation and expiry are checked again after response completion. Errors or unknown usage stop further generation.
- Fixed source and separate evaluation ground-truth snapshots for each experiment. Another 12 independently authored synthetic held-out cases have not been used to tune model prompts.

All source materials are synthetic files created for this project. Public tools can only list the case index, read public materials for that case, and write its report. Evaluation ground truth and fault plans are not included in model requests.

## Usage and metrics

1. Select a source suite, cases, a strategy, repetitions, and budgets, then start the experiment. Switching suites resets selection to all cases in the new suite; cases from the two suites are not mixed.
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
.\.venv\Scripts\python.exe scripts/verify_local.py
.\.venv\Scripts\python.exe scripts/verify_core.py
.\.venv\Scripts\python.exe scripts/verify_offline.py
.\.venv\Scripts\python.exe scripts/verify_scaling.py
.\.venv\Scripts\python.exe scripts/verify_polling.py
.\.venv\Scripts\python.exe scripts/plan_model_experiments.py
~~~

Place `--data-dir` and `--suite` before the subcommand. The web app defaults to v1; switch to heldout-v1 using the suite selector. CLI `--suite` also sets the default suite for `serve`. Both suites share the execution queue, capacity, and cancellation control. Runs and replays retain their own suite hashes; experiments from different suites cannot be formally compared. The model experiment planner only writes a plan and cannot make requests.

`verify_local` checks the full core, ASGI API, cache, and simulated async transport while blocking live HTTP transports; results are stored in `docs/LOCAL_VERIFICATION_V032.json`. `verify_core` checks only the original core and synchronous simulated-provider behavior, writing `docs/CORE_VERIFICATION.json`. `verify_offline` creates a separate `data/verification/acceptance-*` directory, saves 180 scripted runs and 120 replays, and updates `docs/verification-summary-v0.3.2-offline.json`. `verify_scaling` checks 106 experiments, 300 runs, 10,000 events, and one large-payload probe in an isolated directory, writing `docs/SCALING_VERIFICATION_V032.json`. Then `verify_polling` reuses that fixed data to alternate between the original scan and cache paths, writing `docs/POLLING_VERIFICATION_V032.json`. All historical verification summaries are preserved.

This iteration passed all **105 local checks**, including 18 API, 7 overview-cache, and 9 async transport checks, with 0 attempted live HTTP transports. Verification passed for 180 scripted runs, 120 replays, and fixed-data pagination, with matching source fingerprints. The recovery strategy passed its contract in 90/90 runs and achieved complete success in 75/90. A roughly 1 MB probe was reconstructed from 74 chunks with a matching hash; its single-event preview response was 1926 bytes versus 1000117 bytes for the full response. Current browser checks covered original/held-out scripted records, synthetic payloads, and corrupt-event diagnostics, with no horizontal overflow at 768 pixels. The previous iteration's full held-out results of 12/12 contract passes and 9/12 complete successes are preserved as historical evidence. These results establish scripted-harness and simulated-interface behavior; real-model capability, token consumption, and cost remain untested.

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
- Large responses remain fully stored in events. Previews and chunks bound network responses and current page text; initial parsing and integrity checks may still read entire payloads. Corrupt events display diagnostic placeholders, and affected runs lose eligibility for effective evaluation and evidence. Repairing database structure corruption is outside this change.
- The cache holds at most 8 experiments, each with at most 600 runs. Active experiments and cache misses still scan all runs, and warm reads check every report's file state. Execution writes anywhere in the database conservatively invalidate snapshots; file checks are not continuous monitoring. Performance records apply only to fixed local data; local API and async simulations do not verify real-provider behavior.
- Local tests cannot establish real-model capability. There is currently no evidence from real requests, actual model token consumption, or real charges.

Development references: [iteration plan](docs/V032_PLAN.md), [iteration report](docs/BUILD_REPORT_V032.md), [v0.3.1 report](docs/BUILD_REPORT_V031.md), [previous phase plan](docs/NEXT_PHASE_PLAN.md), [task list](docs/NEXT_PHASE_TASKS.md), [previous build report](docs/BUILD_REPORT_V02_V03.md), [original build report](docs/BUILD_REPORT.md), and [held-out freeze manifest](docs/HELDOUT_FREEZE.json). These detailed project documents are currently in Chinese.

# Agent 故障演练场

**中文** | [English](README.en.md)

**工具失败后，沿执行记录核查任务、事实与引用。**

更新：2026-10-11（Asia/Shanghai）。v0.3.2-dev 增加已结束实验概览缓存和按需事件分段浏览；本地全量 105 项检查通过。同一固定数据上，50 条概览页热读中位耗时由 214 毫秒降至 97 毫秒。按用户要求，真实模型测试继续暂停，外部模型请求为 0。实施范围见 [本版方案](docs/V032_PLAN.md)，验证证据见 [本版构建报告](docs/BUILD_REPORT_V032.md)。

线上源码：[GitHub 私有仓库](https://github.com/Halo021501/agent-failure-lab)，默认分支 main。仓库的 GitHub Actions 关闭，本次推送不运行云端构建或测试；详情见 [仓库交付说明](docs/REPOSITORY_DELIVERY.md)。后续开发遵循 [项目约定](AGENTS.md)：中英文 README 同步维护，仓库简介保持中英对照。

## 启动与当前服务

本机已具备项目独立依赖：

~~~powershell
Set-Location -LiteralPath 'E:\Fun projects\03-agent-failure-lab'
.\start.ps1
~~~

访问 [Agent 故障演练场](http://127.0.0.1:8767)。已有进程占用端口时，start.ps1 会提示；直接打开现有页面，或先在原服务终端按 Ctrl+C，再运行脚本。新环境先执行 setup.ps1，依赖锁为 requirements.lock.txt。仓库改名后，本机项目目录仍使用编号 03；从 GitHub 克隆时可使用 agent-failure-lab 目录。

**2026-10-11 已启动并核验 v0.3.2-dev 本机服务。** 网页可选择原套件或独立留出套件，展开事件后按段查看载荷，也可查看非敏感模型配置。页脚显示实际服务版本；后续修改 Python 源码后需重启服务。服务重启本身不会执行模型请求。

## 已实现功能

- 原 30 案例与 12 个独立留出案例保持冻结；网页可切换套件，两条脚本策略保留明确的故障激活与重试记录。
- 报告和执行事件分别核验哈希；完整性异常取消当前评测资格，保留历史结果及独立审计。
- 提交意图、文件原子替换、索引与提交事件事务；同键同内容重试仅提交一次。
- 已提交但回执丢失的失败记录能重现副作用；无产物失败也能严格回放。
- 全库原子队列容量、条件认领、取消竞争保护、合法终态与实时步骤。
- 实验和运行分页、事件增量加载；中文报告、折叠 JSON、实际来源定位与读取事件导航。
- 已结束实验复用有界概览缓存；数据库修订或报告文件状态变化时失效，导出和比较仍强制核验。
- 网页事件先返回 512 字符预览，展开后每段最多 4096 个 Unicode 码点，上一段/下一段替换当前文本；SHA256 防止混合不同载荷快照。原完整事件 API 保持兼容。
- 损坏事件以诊断占位显示，保留序号及原载荷哈希；严格评测、回放和证据入口拒绝损坏记录，不修改原事件。
- 追加式重新评测，保留原结果；逐事实、遗漏、错误、引用、字段组及规则覆盖各自统计。
- 比较条件不一致或整组不完整时不给提升标签；JSON/CSV 和 CLI 共用统计逻辑。
- OpenAI Responses 工具循环、最小/有限恢复两版提示词、非敏感本地配置和环境变量凭据。
- 输入 Token 预检、输出限制、单案/整批预算、在途合作取消；客户端初始化也计入期限，响应完成后再次检查取消/超时；异常和用量未知时停止继续生成。
- 每组实验保存固定资料与独立评测真值快照。另有 12 个独立合成留出案例，未用于模型提示词调优。

资料均为项目自建的合成文件。公开工具只能列索引、读本案公开资料、写本次报告；评测真值与故障计划不会进入模型请求。

## 使用与指标

1. 选择资料套件、案例、策略、次数及预算，点击运行。切换套件后重新全选，不混用两组案例。
2. 打开一条运行，查看中文报告、完整性、评测断言及时间线。
3. 点击引用定位本次实际读取的原文，再跳到读取事件。
4. 建立同条件第二组，查看逐案比较或导出。

完整成功与处置通过分开统计：正确 partial 可通过处置验收，但不计完整成功。处置率分母仅包含完整性有效的自然结束运行（completed/failed/budget_exceeded）；未启动取消不进入耗时。计划/执行/结束覆盖另列，取消或中断使正式整组比较不可用。自然结束失败仍计入事实和引用分母；已提交报告的事实照实评测，但不计任务成功，没有报告的失败才计全部遗漏。

事实计数包含版本、每项功能、每项未解决问题及三个测试统计；合法空列表有单独事实。字段组仍为版本/功能/问题/测试四组。引用分母包含版本、功能、问题（空问题要求读取依据）和目标测试数据行，重复内容不提高分数。旧 v1 评测缺少新口径时显示未知，可重新评测。

| 模式 | 行为 |
| --- | --- |
| trace_replay | 核验并复制封存事件、报告与历史评测；本次工具/模型执行为 0，排除能力指标 |
| tool_replay | Agent 重新决策；逐次严格匹配录制工具和参数，无真实工具回退；模型 Agent 会生成新的模型请求与用量 |
| fresh_run | 从头执行固定资料与故障计划；当前套件必须与来源条件匹配 |

网关负责拒绝重复/旧交付；Agent 负责重试、备用资料与提交回执确认。脚本和模拟供应商结果仅用于框架验收。

## CLI 与免费离线验证

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

--data-dir、--suite 放在子命令之前。网页默认原 v1 套件，可从“资料套件”切换到 heldout-v1。CLI 的 --suite 也决定 serve 启动后的默认套件。两套件共用执行队列、容量和取消控制，运行/回放绑定各自套件哈希，不同套件实验不能正式比较。模型实验规划脚本只写计划，不能发起请求。

verify_local 检查全量核心、ASGI API、缓存与异步模拟传输，并阻断真实 HTTP 传输，结果在 docs/LOCAL_VERIFICATION_V032.json。verify_core 仅检查原核心与同步模拟供应商，结果在 docs/CORE_VERIFICATION.json。verify_offline 另建 data/verification/acceptance-*，保存 180 次脚本实验和 120 次回放，更新 docs/verification-summary-v0.3.2-offline.json。verify_scaling 在独立目录检查 106 实验、300 次运行、1 万事件和一个大载荷探针，结果在 docs/SCALING_VERIFICATION_V032.json。随后 verify_polling 复用该固定数据，交替测量原扫描与缓存路径，结果在 docs/POLLING_VERIFICATION_V032.json；所有历史验证摘要保留。

本版全量 105 项本地检查通过，含 18 项 API、7 项概览缓存和 9 项异步模拟传输，真实 HTTP 传输尝试为 0。180 次脚本实验、120 次回放及固定分页检查通过，源码指纹一致。恢复策略 90/90 处置通过、75/90 完整成功。约 1 MB 探针分为 74 段重建，哈希一致；单条事件预览响应为 1926 字节，原响应为 1000117 字节。当前浏览器检查原/留出脚本记录、合成载荷和损坏诊断，768 像素窗口无横向溢出。上一版留出整套 12/12 处置通过、9/12 完整成功的历史证据另存保留。这些结果只证明脚本框架与模拟接口行为；真实模型能力、Token 和费用仍未经实测。

## 模型配置（真实测试暂缓）

config/model.json 只保存模型名称、环境变量名称和预算；不保存密钥，不预选模型。网页配置或以下 CLI 可修改非敏感设置：

~~~powershell
.\.venv\Scripts\python.exe -m lab model-config
.\.venv\Scripts\python.exe -m lab model-config --model <model_name>
~~~

未来启用时，在启动服务的终端设置 OPENAI_API_KEY 环境变量，随后重启服务。无需向聊天发送密钥。模型执行需明确选择模型执行方式，或使用 CLI run --agent model --strategy model-recovery；默认执行方式始终为 script。本次没有执行这些模型命令。

适配契约、用量与取消限制见 [模型接入说明](docs/MODEL_INTEGRATION.md)。252 次计划尚未启动；两版提示词各有原套件 90 次和留出 36 次，见 [机器计划](docs/MODEL_EXPERIMENT_PLAN.json)。Token 上限是保护限制，不能当作消耗或费用估算。

## 数据、验收边界与资料

SQLite 位于 data/runtime/lab.db，报告在 runs/<run_id>/，固定资料快照在 suites/<suite_hash>/。CLI 与网页同时运行时请使用独立 --data-dir；同目录的多进程执行尚未支持。旧 schema v1 首次迁移时一致性备份数据库和报告。重启后保留记录，未结束运行标为 interrupted，不自动续跑。

- 本地文件 I/O 在调用边界合作取消，无法强制终止卡住的系统调用。
- 外部请求取消会中止本地等待，供应商端可能仍执行或计费；未取得用量时保守保留预算，费用未知。
- 供应商实际 Token 超过预检/输出限制时记录实际值并停止；本地预算不能代替供应商账户消费上限。
- 大响应仍完整保存在事件中；预览与分段限制网络返回和当前页面文本，首次解析及完整性核验仍可能读取全载荷。损坏事件提供诊断占位，相关运行失去有效评测与证据资格；数据库结构本身损坏不在此修复范围。
- 缓存最多保留 8 个实验，每个不超过 600 次运行；活动实验和缓存失效后仍扫描全部运行，热读也检查所有报告文件状态。同库任何执行写入会保守地使缓存失效，文件检查不是连续监控。性能记录仅适用于固定本机数据集，本地 API/异步模拟不验证真实供应商行为。
- 本地测试不能证明真实模型能力，当前真实请求、实际 token 与真实费用证据均无。

开发入口：[本版方案](docs/V032_PLAN.md)、[本版报告](docs/BUILD_REPORT_V032.md)、[v0.3.1 报告](docs/BUILD_REPORT_V031.md)、[前一阶段方案](docs/NEXT_PHASE_PLAN.md)、[任务清单](docs/NEXT_PHASE_TASKS.md)、[前版报告](docs/BUILD_REPORT_V02_V03.md)、[历史首版报告](docs/BUILD_REPORT.md)、[留出冻结清单](docs/HELDOUT_FREEZE.json)。

# OpenAI Responses 适配 · 开发验收说明

更新：2026-10-08。供应商已由用户确认为 OpenAI Responses API；用户随后要求先不进行消耗额度的真实测试。本次外部模型请求为 0，模拟测试中的 Token 为测试夹具值，不能用作实际消费或能力证据。

## 契约及文件

适配直接使用锁定的 httpx 0.28.1，访问官方 HTTPS 端点。响应中的 function_call 经 call_id 映射为 function_call_output；多个调用按顺序执行。三个严格函数分别列公开索引、读取来源和保存报告，模型错误参数原样记录并返回错误，由下一次模型响应自行修正。协议依据 [OpenAI 函数调用文档](https://developers.openai.com/api/docs/guides/function-calling)。

每次生成前请求 responses/input_tokens 预检包含完整历史、指令与工具定义的输入；预检异常不发起生成。每次响应输出上限还包含推理 Token，按供应商返回的输入/输出用量记账。参见 [Token 预检文档](https://developers.openai.com/api/docs/guides/token-counting) 与 [Responses 创建参数](https://developers.openai.com/api/reference/python/resources/responses/methods/create)。

| 文件 | 职责 |
| --- | --- |
| lab/model_config.py、config/model.json | 非敏感配置、环境变量是否已读取、严格预算校验 |
| lab/model_protocol.py | release-check-prompts-v1，两版提示词、三个严格工具定义 |
| lab/model_transport.py | openai-responses-http-v2；官方端点、零自动重试、在途取消、单次/整体超时和完成后复查 |
| lab/agents/model.py | 工具调用循环、可见事件、供应商 ID、预检与用量结算 |
| lab/storage.py | 单批 Token/调用数原子预留，未知用量保守保留 |
| tests/test_model.py | 同步模拟供应商；不创建网络连接 |
| tests/test_transport.py | 9 项异步 HTTP mock 检查已通过；涵盖请求前、客户端初始化、在途及响应完成的取消/超时 |

## 两版提示词及真值边界

model-minimal 每个主资料仅尝试一次，不读备用、不重试写入。model-recovery 允许预算内有限重试和备用资料，确认相同幂等键/相同内容的提交回执。共同任务为提取目标版本、功能、未解决问题、测试统计与实际来源引用。

请求只包含公开任务、目标版本、预算、工具定义，以及模型之前的可见输出和实际工具交付。没有评测真值、expected_counts、可恢复标签或故障计划。框架不自动解析资料给模型、不补齐事实、不改参数、不补写报告。网关继续处理重复/旧信封；模型自行决定重试、备用和报告内容。max_read_attempts 控制脚本策略并写入模型提示词，模型的该策略遵从由其自身决定；硬限制由总工具、模型、网络、Token 及墙钟预算保障。

评测器 facts-and-citations-v3 可在评测阶段检查实际读到的坏格式/空资料，以验证 partial 缺口；这一步不反馈解析答案给模型。它也防止非法来源参数破坏评测。评测版本及来源指纹保存在记录中，历史结果保持不变。

## 记录与预算

model_request、model_response、model_tool_dispatch、model_error 和 Token 预检事件记录可见请求/响应、参数、调用 ID、哈希与耗时。store=false；供应商返回的加密推理内容仅在内存中传递，不写入事件，也不解密。因而日志能复查可见行为，不能重建隐含推理。

输入 Token 预算按预检保守预留；实际值大于预检则补记并停止。输出按单次上限预留，取得有效用量后释放未用部分；取得不到用量则保留全额预留并停止继续生成。整批预留在 SQLite 事务内完成，不因并发提交超额发起请求。

模型调用上限、网络请求上限、包括快照初始化的墙钟时限、工具尝试上限均会检查；网络次数包含预检。模型调用数指已经发起的生成尝试，不代表每次均收到响应或一定计费。已知用量与未知请求数分别记录，费用始终为 unknown，因为尚未配置并验证模型价格。

trace_replay 复制历史且本次执行调用为零；工具回放重新调用模型、严格匹配原工具/参数，模型用量重新计入。匹配失败不会转向真实工具补救。

## 取消与尚未验证的限制

本地异步请求每约 50 毫秒检查取消/期限，并取消等待任务。取消不能证明供应商端停止执行或免收费；未知用量按上述保守规则处理。HTTP 429、超时、网络错误和不完整响应均不自动重试，HTTP 错误消息不保存认证头或原始错误正文。

同步模拟已覆盖多调用、错误参数、缺失用量、输入/输出/调用预算、批量竞争、取消、回执丢失及严格/工具回放。v0.3.1-dev 的全量本地 94 项检查通过，包含 14 项 ASGI API 与 9 项异步 HTTP mock；真实 HTTP 传输在检查期间被阻断，尝试次数为 0。记录见 [LOCAL_VERIFICATION_V031.json](LOCAL_VERIFICATION_V031.json)。新版本机服务已启动，真实供应商接口仍未验收。

客户端初始化耗时也计入请求/运行时限；初始化后过期不发请求，响应完成后再次检查取消与截止时间，防止停止之后派发工具动作。异步测试用明确的请求启动信号和模拟时钟区分“未发送”与“在途”，不会要求尚未启动的任务执行 finally。该项目保留显式零自动重试；官方 SDK 提供独立的重试/超时设置，参见 [OpenAI 官方 Python 文档](https://developers.openai.com/api/reference/python)。本项目没有切换 SDK，也不根据本地取消推断供应商免计费。

当前未发起六案试运行，也未启动 252 次真实实验。MODEL_EXPERIMENT_PLAN.json 明确记录 0 次执行、暂停原因、保护上限和未知估算。只有用户日后明确恢复真实测试并准备配置后，才执行该阶段。

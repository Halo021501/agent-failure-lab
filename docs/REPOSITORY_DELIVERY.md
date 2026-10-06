# 私有仓库交付说明

2026-10-06（Asia/Shanghai）。用户授权推送现有项目，并要求不做占用 GitHub 额度的测试。

仓库：[Halo021501/03-agent-failure-lab](https://github.com/Halo021501/03-agent-failure-lab)。可见性为 private，默认分支 main。代码版本为 0.3.0-dev，保留未完成验收项，不声明完整发布。

## 推送范围

包含 lab/、web/、tests/、scripts/、config/ 非敏感默认配置、30 个原套件案例、12 个冻结留出案例、独立评测资料，以及文档、已有验证摘要和界面截图。config/model.json 未设置模型名称，密钥通过本机环境变量加载。

忽略 .venv/、data/ 的本机数据库/运行产物/验证临时目录、Python 缓存、日志与 .env 文件。已有运行的完整事件和报告仍在本机；仓库中的验证摘要保留原本机数据路径，用于追溯，不能视为 Git 中已有完整运行数据。

## GitHub 用量限制

首次推送前关闭仓库 GitHub Actions。项目不添加 Actions 工作流，也不触发云端测试或构建；提交附带 [skip ci]。本次交付只进行 Git 文件检查、提交、推送和远程分支/仓库设置核对，不执行任何测试或真实模型请求。

## 保留的验证证据

前一轮本地验证为 71 项核心与模型模拟测试通过，180 次脚本实验和 120 次回放验证通过。源代码指纹为 `b9567f736a0fce23c28f4d0c1b5252f5a7392ffda19e13c561a102365b18a065`。

这些结果见 [本轮构建报告](BUILD_REPORT_V02_V03.md)、[核心验证](CORE_VERIFICATION.json) 与 [离线摘要](verification-summary-v0.3-offline.json)。本次推送未重跑测试，也不将历史结果当作最新 HTTP/异步传输或真实模型验收。

12 项 HTTP/API、4 项异步传输模拟、大样本 API 性能和重启后的 v0.3 界面仍待验证。真实模型实验按用户要求暂停，实际执行 0/252。
